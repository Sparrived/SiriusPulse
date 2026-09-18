"""Built-in autonomous-life TOOL.

This TOOL gives a persona a life of her own.  On a slow tick it looks at what she
has already encountered, decides *by pure rule* whether anything is worth
pursuing, and only then spends an LLM turn on it.  Nobody asked, and the result
is material for herself: making and telling are two separate decisions, so this
TOOL never sends anything to a chat.

Options are read from this TOOL's per-persona data store, so they can be tuned in
the WebUI without restarting the persona.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sirius_pulse.core.autonomy import AutonomyPolicy, Episode, build_seed
from sirius_pulse.extension_runtime import BackgroundTaskSpec
from sirius_pulse.memory.units.models import MemoryUnit
from sirius_pulse.utils.json_io import atomic_write_json

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_SECONDS = 900
_DEFAULT_ATTEMPT_COOLDOWN_SECONDS = 3600
_DEFAULT_DAILY_EPISODE_BUDGET = 3
_MIN_INTERVAL_SECONDS = 60

_MAX_EPISODES = 200
_RECENT_MESSAGE_COUNT = 20
_MAX_SEEDS = 8
_OUTCOME_MAX_CHARS = 600

# The persona is explicitly allowed to decline.  A declined turn is not an
# episode: autonomy that must always produce something is just a cron job.
_DECLINE_RE = re.compile(r"^\s*(什么也不做|什么都不做|无事可做|没有想做的|没什么想做的)")

TOOL_META = {
    "name": "autonomy",
    "description": (
        "人格的自主时间。定期查看她留意到的素材，按规则决定是否自己去做点什么"
        "（查资料、读文章、写点东西、整理想法……），并把结果留给她自己的记忆。"
        "该 TOOL 不会向任何群或私聊发送消息。"
    ),
    "version": "1.0.0",
    "model_visible": False,
    "side_effect": "external_write",
    "tags": ["autonomy", "persona", "memory", "background"],
    "config": {
        "check_interval_seconds": {
            "type": "int",
            "description": "自主思考的检查间隔秒数，最少 60，默认 900。",
            "default": _DEFAULT_INTERVAL_SECONDS,
            "group": "节奏",
        },
        "attempt_cooldown_seconds": {
            "type": "int",
            "description": "两次真正调用模型的自主回合之间的最小间隔秒数，默认 3600。",
            "default": _DEFAULT_ATTEMPT_COOLDOWN_SECONDS,
            "group": "节奏",
        },
        "daily_episode_budget": {
            "type": "int",
            "description": "每天最多发生的自主事件数量，默认 3；设为 0 表示停用自主行为。",
            "default": _DEFAULT_DAILY_EPISODE_BUDGET,
            "group": "限制",
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


async def run_tick(ctx: Any) -> Episode | None:
    """Run one autonomy tick.  Returns the episode, or None when she did nothing."""
    store = ctx.get_data_store("autonomy")
    reload_store = getattr(store, "reload", None)
    if callable(reload_store):
        reload_store()

    if store.get("_enabled", True) is False:
        return None
    if _daily_budget(ctx, store) <= 0:
        return None

    state = _load_state(store)
    now = datetime.now(timezone.utc)
    # 每日预算是硬上限，不交给打分去"大概率"拦住。
    if _episodes_today(state, now) >= _daily_budget(ctx, store):
        return None
    cooldown = timedelta(
        seconds=_option_seconds(ctx, "attempt_cooldown_seconds", _DEFAULT_ATTEMPT_COOLDOWN_SECONDS)
    )
    if now - _parse_time(state.get("last_attempt_at", "")) < cooldown:
        return None

    group_id, seeds = _collect_seeds(ctx, state)
    if not group_id or not seeds:
        return None

    decision = AutonomyPolicy().evaluate(
        seconds_since_episode=(now - _parse_time(state.get("last_episode_at", ""))).total_seconds(),
        seeds=seeds,
        recent_kinds=list(state.get("recent_kinds", [])),
        episodes_today=_episodes_today(state, now),
        daily_episode_budget=_daily_budget(ctx, store),
        expressiveness=ctx.get_expressiveness(),
    )
    logger.debug("自主性评估: %s", decision.to_dict())
    if not decision.should_act:
        return None

    # From here on a model turn happens, so the attempt itself is booked even if
    # she then decides there is nothing she wants to do.
    state["last_attempt_at"] = now.isoformat()
    _save_state(store, state)

    result = await ctx.run_autonomous_turn(
        kind=decision.kind,
        seed=decision.seed,
        group_id=group_id,
    )
    outcome = str(result.get("text", "") or "").strip()
    if not outcome or _DECLINE_RE.match(outcome):
        logger.info("自主回合未产出内容，跳过记录: %s", decision.reason)
        return None

    episode = Episode(
        episode_id=uuid.uuid4().hex,
        started_at=now.isoformat(),
        ended_at=datetime.now(timezone.utc).isoformat(),
        kind=decision.kind,
        seed=decision.seed,
        outcome=outcome[:_OUTCOME_MAX_CHARS],
        refs=_extract_refs(decision.seed),
        intensity=decision.score,
        status="done",
    )
    _append_episode(ctx, episode)
    _write_memory_unit(ctx, group_id, episode)

    state["last_episode_at"] = episode.ended_at
    state["episode_count_date"] = _date_key(now)
    state["episode_count_today"] = _episodes_today(state, now) + 1
    state["recent_kinds"] = [episode.kind, *list(state.get("recent_kinds", []))][:5]
    _save_state(store, state)

    ctx.log_inner_thought(f"我自己去{episode.kind}了：{episode.outcome[:60]}")
    await ctx.emit_event(
        "agent_turn_updated",
        {"origin": "self_initiated", "phase": "complete", "episode": episode.to_dict()},
    )
    return episode


def _collect_seeds(ctx: Any, state: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """Gather material she already encountered, plus anything left unfinished."""
    seeds: list[dict[str, Any]] = []
    for episode in state.get("active_episodes", [])[:_MAX_SEEDS]:
        if str(episode.get("seed", "")).strip():
            seeds.append(build_seed(episode["seed"], kind=str(episode.get("kind", "")), weight=1.0))

    # 素材取自最近真正聊过话的群：活跃群列表按首次出现排序，不能直接取末位。
    group_id = ""
    recent: list[dict[str, Any]] = []
    best_stamp = ""
    for candidate in (str(item) for item in ctx.get_active_groups()):
        if not candidate.strip():
            continue
        messages = ctx.get_recent_messages(candidate, _RECENT_MESSAGE_COUNT)
        if not messages:
            continue
        stamp = str(messages[-1].get("timestamp", "") or "")
        if not group_id or stamp > best_stamp:
            group_id, recent, best_stamp = candidate, messages, stamp

    for message in recent[-_MAX_SEEDS:]:
        content = str(message.get("content", "") or "").strip()
        if not content or message.get("role") == "assistant":
            continue
        seeds.append(build_seed(content, source="chat"))
    return group_id, seeds[:_MAX_SEEDS]


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
    """Feed the outcome into existing memory so retrieval surfaces it in chat."""
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
        metadata={"origin": "self_initiated", "episode_id": episode.episode_id},
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


def _daily_budget(ctx: Any, store: Any) -> int:
    raw = store.get("daily_episode_budget")
    if raw is None:
        raw = TOOL_META["config"]["daily_episode_budget"]["default"]
    try:
        return int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_DAILY_EPISODE_BUDGET


def _option_seconds(ctx: Any, key: str, default: int) -> float:
    store = ctx.get_data_store("autonomy")
    raw = store.get(key, default)
    try:
        return max(_MIN_INTERVAL_SECONDS, float(raw))
    except (TypeError, ValueError):
        return float(default)


def _episodes_today(state: dict[str, Any], now: datetime) -> int:
    if state.get("episode_count_date") != _date_key(now):
        return 0
    try:
        return int(state.get("episode_count_today", 0))
    except (TypeError, ValueError):
        return 0


def _date_key(now: datetime) -> str:
    return now.date().isoformat()


def _parse_time(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))
