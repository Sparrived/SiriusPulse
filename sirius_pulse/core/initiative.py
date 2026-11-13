"""她在群里的主动：有分寸的那一部分。

自主性大头在不打扰别人的地方（她自己的生活、和主人的私聊）。群里的主动只开一条
很窄的通道，规则如下：

- **档位**：每个群 ``off`` / ``quiet`` / ``normal``，默认 ``normal``。档位决定每天、
  每小时最多主动几次。
- **只对熟人、顺势而为**：目前唯一的群内主动是「惦记的事，等对方出现时顺势问一句」。
  对方自己在群里冒头时才可能发生，不会凭空开新话题，也不会对陌生人或别的 Bot 发起。
- **不打断**：群里正热闹（几个人在你来我往）时不插进去。
- **自动收敛**：连续两次主动都没人接，当天额度减半，直到有人接话；有人对她说
  「别吵」「安静点」之类，立刻在这个群静默两小时。

这里只有规则和记账，不调用模型；真正说什么由 :class:`InitiativeService` 交给一次
普通的回复回合决定，模型也可以选择不说（``[SKIP]``）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sirius_pulse.utils.json_io import atomic_write_json

logger = logging.getLogger(__name__)

LEVEL_OFF = "off"
LEVEL_QUIET = "quiet"
LEVEL_NORMAL = "normal"
LEVELS = (LEVEL_OFF, LEVEL_QUIET, LEVEL_NORMAL)
DEFAULT_LEVEL = LEVEL_NORMAL


@dataclass(frozen=True, slots=True)
class Budget:
    per_day: int
    per_hour: int


BUDGETS = {
    LEVEL_OFF: Budget(per_day=0, per_hour=0),
    LEVEL_QUIET: Budget(per_day=3, per_hour=1),
    LEVEL_NORMAL: Budget(per_day=8, per_hour=2),
}

#: 主动之后这么久之内有人接话（叫她、或被问的人开口）就算被回应了。
ANSWER_WINDOW = timedelta(minutes=10)
#: 连续这么多次没人接，当天额度减半。
IGNORED_STREAK = 2
HUSH_DURATION = timedelta(hours=2)
#: 群里在热聊：最近 5 分钟有这么多个不同的人在说话，或 3 分钟内有这么多条消息。
BUSY_SPEAKERS = 4
BUSY_MESSAGES = 10
#: 对方出现后等一会儿再开口，像人看到消息、想一想再说，也给她正常回复留出机会。
FOLLOW_UP_DELAY_SECONDS = (45.0, 120.0)
#: 没有日期的惦记，至少放一天再问，免得刚听说就追问。
UNDATED_THREAD_MIN_AGE = timedelta(days=1)
_MAX_ACTS_KEPT = 50

#: 对她说这些话就是让她安静一会儿（只在明确叫到她时生效）。
HUSH_PHRASES = (
    "别吵",
    "安静点",
    "安静一点",
    "安静会",
    "闭嘴",
    "别说话",
    "少说两句",
    "别插话",
    "别刷屏",
    "shut up",
)


def normalize_level(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in LEVELS else DEFAULT_LEVEL


def level_for(config: dict[str, Any] | None, group_id: str) -> str:
    """群级档位优先，其次是人格默认档位。"""
    config = config or {}
    per_group = config.get("group_initiative_levels") or {}
    if isinstance(per_group, dict) and group_id in per_group:
        return normalize_level(per_group[group_id])
    return normalize_level(config.get("initiative_level", DEFAULT_LEVEL))


def asks_for_quiet(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(phrase in lowered for phrase in HUSH_PHRASES)


def _parse(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def is_busy(entries: list[Any], now: datetime) -> bool:
    """群里是否正热闹：几个人在你来我往时，她不插进去开别的话题。"""
    recent_speakers: set[str] = set()
    burst = 0
    for entry in entries:
        if getattr(entry, "role", "") == "assistant":
            continue
        stamp = _parse(getattr(entry, "timestamp", ""))
        if stamp is None:
            continue
        if now - stamp <= timedelta(minutes=5):
            recent_speakers.add(str(getattr(entry, "user_id", "") or ""))
        if now - stamp <= timedelta(minutes=3):
            burst += 1
    return len(recent_speakers) >= BUSY_SPEAKERS or burst >= BUSY_MESSAGES


class InitiativeLedger:
    """每个群她主动过几次、有没有人接、是否被要求安静。持久化到一个小 JSON。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._groups: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        groups = data.get("groups") if isinstance(data, dict) else None
        if isinstance(groups, dict):
            self._groups = {str(k): v for k, v in groups.items() if isinstance(v, dict)}

    def save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self._path, {"groups": self._groups})
        except OSError:
            logger.warning("保存主动记账失败", exc_info=True)

    def _group(self, group_id: str) -> dict[str, Any]:
        group = self._groups.setdefault(group_id, {})
        group.setdefault("acts", [])
        group.setdefault("hush_until", "")
        return group

    def acts(self, group_id: str) -> list[dict[str, Any]]:
        return list(self._group(group_id)["acts"])

    def hushed(self, group_id: str, now: datetime) -> bool:
        until = _parse(self._group(group_id).get("hush_until"))
        return until is not None and now < until

    def hush(self, group_id: str, now: datetime) -> None:
        self._group(group_id)["hush_until"] = (now + HUSH_DURATION).isoformat()

    def blocked_reason(self, group_id: str, now: datetime, level: str) -> str:
        """为什么现在不能主动；可以时返回空串。"""
        budget = BUDGETS.get(normalize_level(level), BUDGETS[DEFAULT_LEVEL])
        if budget.per_day <= 0:
            return "off"
        if self.hushed(group_id, now):
            return "hushed"
        acts = self._group(group_id)["acts"]
        times = [(_parse(act.get("at")), act) for act in acts]
        today = [act for stamp, act in times if stamp and now - stamp < timedelta(days=1)]
        last_hour = [act for stamp, act in times if stamp and now - stamp < timedelta(hours=1)]
        per_day = budget.per_day
        if self._ignored_streak(acts, now) >= IGNORED_STREAK:
            per_day = max(1, per_day // 2)
        if len(today) >= per_day:
            return "daily_budget"
        if len(last_hour) >= budget.per_hour:
            return "hourly_budget"
        return ""

    @staticmethod
    def _ignored_streak(acts: list[dict[str, Any]], now: datetime) -> int:
        streak = 0
        for act in reversed(acts):
            stamp = _parse(act.get("at"))
            if stamp is None or now - stamp < ANSWER_WINDOW:
                continue  # 还在等回应，不算被忽略
            if act.get("answered"):
                break
            streak += 1
        return streak

    def record(self, group_id: str, now: datetime, *, kind: str, target: str) -> None:
        acts = self._group(group_id)["acts"]
        acts.append({"at": now.isoformat(), "kind": kind, "target": target, "answered": False})
        del acts[:-_MAX_ACTS_KEPT]

    def note_message(self, group_id: str, user_id: str, now: datetime, *, addressed: bool) -> bool:
        """有人说话了：如果是在回应她最近的主动，就记为被接住。返回是否有变化。"""
        changed = False
        for act in self._group(group_id)["acts"]:
            stamp = _parse(act.get("at"))
            if act.get("answered") or stamp is None or not (0 <= (now - stamp).total_seconds()):
                continue
            if now - stamp > ANSWER_WINDOW:
                continue
            if addressed or (user_id and user_id == act.get("target")):
                act["answered"] = True
                changed = True
        return changed


class InitiativeService:
    """把规则接到引擎上：看每条进来的消息，必要时排一次「顺势问一句」。"""

    def __init__(self, engine: Any) -> None:
        self._engine = engine
        work_path = Path(getattr(engine, "work_path", ".") or ".")
        self.ledger = InitiativeLedger(work_path / "engine_state" / "initiative.json")
        self._pending: dict[tuple[str, str], asyncio.Task[Any]] = {}

    # ------------------------------------------------------------------
    # 入口：每条人类消息都会经过这里（只做记账与判断，不阻塞回复）
    # ------------------------------------------------------------------

    def on_message(self, group_id: str, user_id: str, message: Any) -> None:
        if not group_id or str(group_id).startswith("private_"):
            return
        try:
            self._on_message(group_id, user_id, message)
        except Exception:
            logger.warning("主动规则处理消息失败，不影响回复", exc_info=True)

    def _on_message(self, group_id: str, user_id: str, message: Any) -> None:
        engine = self._engine
        now = datetime.now(timezone.utc)
        mentions = getattr(engine, "_message_explicitly_mentions_current_bot", None)
        addressed = bool(mentions(message)) if callable(mentions) else False
        changed = self.ledger.note_message(group_id, user_id, now, addressed=addressed)
        text = str(getattr(message, "content", "") or "")
        if addressed and asks_for_quiet(text):
            self.ledger.hush(group_id, now)
            changed = True
            log = getattr(engine, "_log_inner_thought", None)
            if callable(log):
                log("被说吵了，这个群里我先安静一会儿，不主动找人说话。")
        if changed:
            self.ledger.save()
        if getattr(message, "sender_type", "") == "other_ai" or not user_id:
            return
        thread = self._thread_to_ask(user_id, now)
        if thread is None:
            return
        level = level_for(getattr(engine, "config", {}), group_id)
        if self.ledger.blocked_reason(group_id, now, level):
            return
        key = (group_id, user_id)
        pending = self._pending.get(key)
        if pending is not None and not pending.done():
            return
        try:
            task = asyncio.get_running_loop().create_task(
                self._follow_up(group_id, user_id, thread, now), name="initiative_follow_up"
            )
        except RuntimeError:
            return
        self._pending[key] = task
        task.add_done_callback(lambda _done, key=key: self._pending.pop(key, None))

    def _thread_to_ask(self, user_id: str, now: datetime) -> str | None:
        from sirius_pulse.memory.impressions import impression_store

        impression = impression_store(self._engine).get(user_id)
        if impression is None:
            return None
        candidates = [t for t in impression.live_threads(now) if not t.asked_at]
        due = [t for t in candidates if t.is_due_soon(now)]
        if due:
            return due[0].text
        for thread in candidates:
            created = _parse(thread.created_at)
            if not thread.due and created is not None and now - created >= UNDATED_THREAD_MIN_AGE:
                return thread.text
        return None

    # ------------------------------------------------------------------
    # 顺势问一句
    # ------------------------------------------------------------------

    async def _follow_up(self, group_id: str, user_id: str, thread: str, seen_at: datetime) -> None:
        await asyncio.sleep(random.uniform(*FOLLOW_UP_DELAY_SECONDS))
        engine = self._engine
        now = datetime.now(timezone.utc)
        level = level_for(getattr(engine, "config", {}), group_id)
        if self.ledger.blocked_reason(group_id, now, level):
            return
        if not self._still_a_good_moment(group_id, seen_at, now):
            return
        # 先记下「问过了」：无论她这次说不说，同一件事都不再反复安排。
        self._mark_asked(user_id, thread, now)
        reply = await self._generate(group_id, user_id, thread)
        if reply is None:
            return
        text, stickers, pokes = reply
        if not (text or stickers or pokes):
            logger.info("她决定这会儿不问：%s", thread)
            return
        dispatch = getattr(engine, "dispatch_proactive_message", None)
        if not callable(dispatch):
            return
        sent = await dispatch(
            group_id=group_id,
            text=text,
            event_id=f"initiative:{group_id}:{user_id}:{int(now.timestamp())}",
            sticker_names=stickers,
            poke_user_ids=pokes,
        )
        if sent is True:
            self.ledger.record(group_id, now, kind="follow_up", target=user_id)
            self.ledger.save()
            log = getattr(engine, "_log_inner_thought", None)
            if callable(log):
                log(f"看到 ta 来了，顺口问了问：{thread}")

    def _still_a_good_moment(self, group_id: str, seen_at: datetime, now: datetime) -> bool:
        engine = self._engine
        if getattr(engine, "_work_mode_runs", {}).get(group_id) is not None:
            return False
        try:
            from sirius_pulse.core import life_bridge

            if life_bridge.reply_factor(engine, now) < 0.5:
                return False  # 睡着了
        except Exception:
            pass
        memory = getattr(engine, "basic_memory", None)
        entries = memory.get_context(group_id, n=30) if memory is not None else []
        for entry in entries:
            stamp = _parse(getattr(entry, "timestamp", ""))
            if getattr(entry, "role", "") == "assistant" and stamp and stamp >= seen_at:
                return False  # 她已经和群里说上话了，惦记的事交给正常回复去提
        return not is_busy(entries, now)

    def _mark_asked(self, user_id: str, thread: str, now: datetime) -> None:
        from sirius_pulse.memory.impressions import impression_store

        store = impression_store(self._engine)
        impression = store.get(user_id)
        if impression is None:
            return
        for item in impression.threads:
            if item.text == thread:
                item.asked_at = now.isoformat()
        store.save(impression)

    async def _generate(
        self, group_id: str, user_id: str, thread: str
    ) -> tuple[str, list[str], list[str]] | None:
        from sirius_pulse.core.brain import ChatRequest
        from sirius_pulse.core.prompt_factory import PromptFactory
        from sirius_pulse.memory.impressions import impression_store, render_impressions

        engine = self._engine
        impression = impression_store(engine).get(user_id)
        name = (impression.name if impression else "") or user_id
        qq = user_id.removeprefix("qq_")
        note = (
            f"【此刻】{name} 刚在群里冒头了。你心里一直惦记着：{thread}。\n"
            "如果这会儿顺口问一句很自然，就用你平时的语气问一两句"
            f"（想叫他可以在开头用 [AT:{qq}]）；"
            f"如果不合适（他正忙着聊别的、这事已经有结果了、你刚问过），只输出 {_skip_tag()}。"
        )
        manager = getattr(engine, "memory_unit_manager", None)
        prepare_async = getattr(manager, "prepare_retrieval_async", None)
        prepare = getattr(manager, "prepare_retrieval", None)
        if callable(prepare_async):
            await prepare_async(thread, group_id=group_id)
        elif callable(prepare):
            await asyncio.to_thread(prepare, thread, group_id=group_id)
        try:
            bundle = PromptFactory.assemble_chat(
                message_content=note,
                content_is_tagged=True,
                style_params=None,
                other_ai_names=list(getattr(engine, "_other_ai_names", []) or []),
                adapter_type="napcat",
                sticker_names=getattr(engine, "_sticker_names", None),
                qq_mention_members=[{"user_id": qq}],
            )
            messages = engine.context_assembler.build_messages(
                group_id=group_id,
                current_query=bundle.user_content,
                system_prompt=bundle.system_prompt,
                search_query=thread,
                content_is_tagged=True,
                dynamic_context=render_impressions([impression]) if impression else "",
                history_token_budget=6000,
            )
            result = await engine.brain.chat(
                ChatRequest(
                    group_id=group_id,
                    user_id=qq,
                    system_prompt=messages[0]["content"],
                    messages=messages[1:],
                    task_name="response_generate",
                    enable_tools=False,
                    post_process=True,
                    inject_current_time=True,
                )
            )
        except Exception as exc:
            logger.warning("顺势问一句的回合失败: %s", exc)
            return None
        text = str(getattr(result, "clean_text", "") or "").strip()
        return (
            text,
            list(getattr(result, "sticker_names", []) or []),
            list(getattr(result, "poke_user_ids", []) or []),
        )


def service_for(engine: Any) -> InitiativeService:
    """引擎共用的群内主动服务（惰性创建并挂在引擎上）。"""
    service = getattr(engine, "_initiative_service", None)
    if not isinstance(service, InitiativeService):
        service = InitiativeService(engine)
        try:
            setattr(engine, "_initiative_service", service)
        except AttributeError:
            pass
    return service


def _skip_tag() -> str:
    from sirius_pulse.core.prompt_factory import TAG_SKIP_REPLY

    return TAG_SKIP_REPLY
