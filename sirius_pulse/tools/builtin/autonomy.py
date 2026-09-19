"""Built-in autonomous-life TOOL.

This TOOL gives a persona a life of her own.  On a slow tick it looks at the
*intentions* she is already carrying and decides by pure rule whether one is
worth acting on now.  The tick never invents a reason to act: no intentions means
she does nothing, however long she has been idle.

Intentions are formed in real turns, not here — ``intend_share`` for something she
wants to say, ``intend_pursue`` for something she wants to work out.  That is what
keeps the reason behind each one genuine: she decided it while actually thinking,
so the recorded ``why`` is hers rather than a constant.  A background job scanning
the chat log cannot know what she has already handled, so it would mostly queue up
material she has already read.

Intentions cannot be the *only* entry point, though, or the whole thing is a closed
loop: every intention would have to come from a reply, so she could never start
anything by herself and a quiet group would leave her permanently silent.  So when
she is carrying nothing and has been left alone for a long while, the tick offers
her **free time** with no material at all ("your time, do as you like").  She may
still decline.  This is the one place a self-initiated turn begins from nothing,
and it is paced by ``free_time_interval_seconds`` so it cannot become a job.

Two things can happen when an intention is picked:

``do``
    She works something out (reads it, tries it, writes it down).  This costs one
    LLM turn, and the result is material for herself.
``tell``
    She says what she wanted to say, to the audience she chose.  The words were
    already written down when the intention formed, so this costs nothing and is
    delivered through the normal proactive-message pipeline.

Making and telling stay separate decisions, and telling is never a broadcast: the
audience is part of the intention itself.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sirius_pulse.core.autonomy import AutonomyPolicy, Episode, is_quiet_hours
from sirius_pulse.core.intent import (
    IntentFileStore,
    Intention,
    IntentStore,
)
from sirius_pulse.extension_runtime import BackgroundTaskSpec
from sirius_pulse.memory.units.models import MemoryUnit
from sirius_pulse.utils.json_io import atomic_write_json

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_SECONDS = 900
_MIN_INTERVAL_SECONDS = 60

_MAX_EPISODES = 200
_OUTCOME_MAX_CHARS = 600

# How long she must have been left to herself before free time is offered.  This
# is the only path that starts from nothing, so it is paced separately from the
# heartbeat: at 900s heartbeats a 1h interval means at most a handful of free-time
# turns a day, not one per beat.
_DEFAULT_FREE_TIME_INTERVAL_SECONDS = 60 * 60

# A tell-intention is cheap to deliver, so it gets its own, much tighter gate:
# one share per hour per persona, on top of the normal reply cooldown.
_DEFAULT_SHARE_COOLDOWN_SECONDS = 3600

# The persona is explicitly allowed to decline.  A declined turn is not an
# episode: autonomy that must always produce something is just a cron job.
_DECLINE_RE = re.compile(r"^\s*(什么也不做|什么都不做|无事可做|没有想做的|没什么想做的)")

TOOL_META = {
    "name": "autonomy",
    "description": (
        "人格的自主时间。定期查看她正在惦记的事（意图），按规则决定是否推进其中一件：" "要么自己去做点什么，要么把想说的话告诉她想告诉的人。" "大多数时候结论是什么都不做。"
    ),
    "version": "2.0.0",
    "model_visible": False,
    "side_effect": "external_write",
    "tags": ["autonomy", "intent", "persona", "memory", "background"],
    "config": {
        "check_interval_seconds": {
            "type": "int",
            "description": "自主思考的检查间隔秒数，最少 60，默认 900。",
            "default": _DEFAULT_INTERVAL_SECONDS,
            "group": "节奏",
        },
        "share_cooldown_seconds": {
            "type": "int",
            "description": "两次主动分享之间的最小间隔秒数，默认 3600。",
            "default": _DEFAULT_SHARE_COOLDOWN_SECONDS,
            "group": "节奏",
        },
        "free_time_interval_seconds": {
            "type": "int",
            "description": (
                "她无事惦记且长时间没人找她时，隔多久给她一段完全空白的自主时间。" "设为 0 表示关闭（她只会在已经惦记着什么时才行动）。默认 3600。"
            ),
            "default": _DEFAULT_FREE_TIME_INTERVAL_SECONDS,
            "group": "节奏",
        },
    },
}


def create_background_tasks(ctx: Any) -> list[BackgroundTaskSpec]:
    """Register the autonomy ticker."""

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


async def run_tick(ctx: Any) -> Episode | None:
    """Run one autonomy tick.  Returns the episode, or None when she did nothing."""
    store = ctx.get_data_store("autonomy")
    reload_store = getattr(store, "reload", None)
    if callable(reload_store):
        reload_store()

    if store.get("_enabled", True) is False:
        return None

    state = _load_state(store)
    now = _now()

    # First sight of a fresh state: start the free-time clock now rather than
    # leaving it absent.  ``_parse_time("")`` reads as 1970, which would hand her
    # a free-time turn on the first tick of every restart.
    if not state.get("last_free_time_at"):
        state["last_free_time_at"] = now.isoformat()
        _save_state(store, state)

    # This tick is a checkpoint, not a source of motivation: it reads what she is
    # already carrying.  Forming intentions happens in real turns (intend_pursue /
    # intend_share), where the conversation is in front of her.
    intentions = _load_intentions(ctx)

    # Sharing is cheap and does not spend an LLM turn, so it is gated separately
    # from model pacing and can happen even while a model turn is cooling down.
    if _share_ready(state, now, ctx):
        share = _next_share(intentions, now)
        if share is not None and await _deliver_share(ctx, share, state, now):
            return _record_share(ctx, state, share, now)

    # No model-call cooldown either: if she is still carrying something, she may
    # act now.  The real pace limit is the heartbeat interval itself, so an empty
    # intention set (not a timer) is what keeps her quiet.
    # Only two kinds of intention are worth a model turn: something to work out,
    # and something she wants to say but has not decided who to tell.  A tell that
    # already has an audience is delivered mechanically, so it must never be
    # re-litigated here — that would rewrite words she has already chosen.
    actionable = [
        item
        for item in intentions.open_items(now=now.isoformat())
        if not (item.is_tell and item.audience.strip())
    ]
    decision = AutonomyPolicy(
        free_time_interval_seconds=_option_seconds(
            ctx, "free_time_interval_seconds", _DEFAULT_FREE_TIME_INTERVAL_SECONDS, minimum=0
        )
    ).evaluate(
        seconds_since_episode=(now - _parse_time(state.get("last_episode_at", ""))).total_seconds(),
        intentions=actionable,
        recent_kinds=list(state.get("recent_kinds", [])),
        expressiveness=ctx.get_expressiveness(),
        now=now.isoformat(),
        # Idle since free time was last *offered*, not since she last acted: a
        # declined offer must not be re-offered on the very next heartbeat.
        seconds_since_free_time=(
            now - _parse_time(state.get("last_free_time_at", ""))
        ).total_seconds(),
    )
    logger.debug("自主性评估: %s", decision.to_dict())
    if not decision.should_act:
        return None

    target = intentions.get(decision.intention_id)
    if decision.intention_id and target is None:
        return None

    # Free time carries no intention: there is nothing to book an attempt against
    # and nothing to resolve afterwards.  It is recorded like an episode so the
    # next offer is paced, nothing more.
    if target is None:
        return await _run_free_time(ctx, state, store, decision, now)

    # From here on a model turn happens, so book the attempt against *this*
    # intention before running it.  That is what bounds the cost now that there is
    # no pacing cooldown: she can always act on something new, but she cannot
    # re-decide the same unfinishable thing on every heartbeat.
    intentions.record_attempt(target.intention_id, now=now.isoformat())
    _save_intentions(ctx, intentions)

    result = await ctx.run_autonomous_turn(
        kind=decision.kind,
        seed=decision.seed,
        group_id=target.origin_group or _fallback_group(ctx),
        why=target.why,
        intention_id=target.intention_id,
        resolution=target.resolution,
    )
    outcome = str(result.get("text", "") or "").strip()

    # The turn may itself have written to the intention store (e.g. she finally
    # decided who to tell).  Re-read before touching it, or that decision would be
    # overwritten by this stale copy — and the words would never be delivered.
    intentions = _load_intentions(ctx)

    if not outcome or _DECLINE_RE.match(outcome):
        logger.info("自主回合未产出内容，保留意图: %s", decision.reason)
        return None

    episode = Episode(
        episode_id=uuid.uuid4().hex,
        started_at=now.isoformat(),
        ended_at=datetime.now(timezone.utc).isoformat(),
        kind=decision.kind,
        seed=decision.seed,
        outcome=outcome[:_OUTCOME_MAX_CHARS],
        intention_id=target.intention_id,
        resolution=target.resolution,
        refs=_extract_refs(decision.seed),
        intensity=decision.score,
        status="done",
    )
    # A `tell` is only finished once it has actually been said, so this turn must
    # not close it: if she still has not picked who to tell, the intention stays
    # open and gets asked again instead of silently disappearing.
    if not target.is_tell:
        intentions.resolve(target.intention_id, outcome=episode.outcome, now=episode.ended_at)
    _save_intentions(ctx, intentions)
    _append_episode(ctx, episode)
    _write_memory_unit(ctx, target.origin_group or _fallback_group(ctx), episode)
    _finish_tick(state, episode)
    _save_state(store, state)

    ctx.log_inner_thought(f"我自己去{episode.kind}了：{episode.outcome[:60]}")
    await ctx.emit_event(
        "agent_turn_updated",
        {"origin": "self_initiated", "phase": "complete", "episode": episode.to_dict()},
    )
    return episode


async def _run_free_time(
    ctx: Any, state: dict[str, Any], store: Any, decision: Any, now: datetime
) -> Episode | None:
    """Give her a stretch of time with nothing in it.

    Her own time in the truest sense: no material, no reason, nobody waiting.  She
    may do something or answer 「什么也不做」.  A decline still counts as the offer
    being spent, so the interval — not the outcome — is what paces this path.
    """
    result = await ctx.run_autonomous_turn(
        kind=decision.kind,
        seed="",
        group_id=_fallback_group(ctx),
        free_time=True,
    )
    outcome = str(result.get("text", "") or "").strip()

    if not outcome or _DECLINE_RE.match(outcome):
        # Nothing came of it, but the offer is used up: remember that, or the very
        # next heartbeat would offer again and bill another turn.
        state["last_free_time_at"] = now.isoformat()
        _save_state(store, state)
        return None

    episode = Episode(
        episode_id=uuid.uuid4().hex,
        started_at=now.isoformat(),
        ended_at=datetime.now(timezone.utc).isoformat(),
        kind=decision.kind,
        seed="",
        outcome=outcome[:_OUTCOME_MAX_CHARS],
        refs=_extract_refs(outcome),
        intensity=decision.score,
        status="done",
    )
    _append_episode(ctx, episode)
    _write_memory_unit(ctx, _fallback_group(ctx), episode)
    state["last_free_time_at"] = episode.ended_at
    _finish_tick(state, episode)
    _save_state(store, state)

    ctx.log_inner_thought(f"我自己的时间：{episode.outcome[:60]}")
    await ctx.emit_event(
        "agent_turn_updated",
        {"origin": "self_initiated", "phase": "complete", "episode": episode.to_dict()},
    )
    return episode


async def run_share_tick(ctx: Any) -> bool:
    """Deliver one pending share out of band.  Used by tests and manual nudges."""
    store = ctx.get_data_store("autonomy")
    state = _load_state(store)
    now = _now()
    intentions = _load_intentions(ctx)
    share = _next_share(intentions, now)
    if share is None:
        return False
    delivered = await _deliver_share(ctx, share, state, now)
    if delivered:
        _record_share(ctx, state, share, now)
    return delivered


async def _deliver_share(ctx: Any, share: Intention, state: dict[str, Any], now: datetime) -> bool:
    """Say what she wanted to say, to the audience she picked."""
    audience = share.audience.strip()
    if not audience:
        # She never picked anyone.  Ask her once, on a model turn, rather than
        # guessing a destination on her behalf.
        return False
    # Quiet hours gate *sending* only, and deliberately not ``last_share_at``:
    # the intention stays pending so it goes out after the window ends instead of
    # being dropped or silently marked as said.  She is still free to think and
    # work during the night -- only the message waits.
    if is_quiet_hours(now):
        logger.debug("夜间静默期，暂不投递分享: %s", audience)
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
    _finish_tick(state, episode)
    _save_state(ctx.get_data_store("autonomy"), state)
    ctx.log_inner_thought(f"我把想说的话告诉了{share.audience_label or share.audience}")
    return episode


def _finish_tick(state: dict[str, Any], episode: Episode) -> None:
    state["last_episode_at"] = episode.ended_at
    state["recent_kinds"] = [episode.kind, *list(state.get("recent_kinds", []))][:5]


def _share_ready(state: dict[str, Any], now: datetime, ctx: Any) -> bool:
    cooldown = timedelta(
        seconds=_option_seconds(ctx, "share_cooldown_seconds", _DEFAULT_SHARE_COOLDOWN_SECONDS)
    )
    return now - _parse_time(state.get("last_share_at", "")) >= cooldown


def _next_share(intentions: IntentStore, now: datetime) -> Intention | None:
    ready = intentions.pending_shares(now=now.isoformat())
    return ready[0] if ready else None


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


def _write_memory_unit(ctx: Any, group_id: str, episode: Episode) -> None:
    """Feed the outcome into existing memory so she *knows* it later.

    This makes the episode part of what she remembers; it is deliberately not a
    delivery channel.  Whether she mentions it is decided elsewhere.
    """
    persona = ctx.get_persona()
    unit = MemoryUnit(
        unit_id=f"autonomy-{episode.episode_id}",
        group_id=group_id,
        created_at=episode.ended_at or episode.started_at,
        unit_type="event",
        scope="persona",
        scope_id=str(getattr(persona, "name", "") or ""),
        summary=f"（我自己的时间）我{episode.kind}了：{episode.outcome}",
        topics=[episode.kind],
        keywords=[episode.kind],
        retrieval_terms=[episode.kind, *episode.refs],
        event_time=episode.ended_at or episode.started_at,
        salience=_clamp(0.4 + episode.intensity * 0.4),
        lifespan="long",
        should_prompt=True,
        metadata={
            "origin": "self_initiated",
            "episode_id": episode.episode_id,
            "first_person_experience": True,
        },
    )
    try:
        ctx.add_memory_unit(unit)
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


def _parse_time(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))
