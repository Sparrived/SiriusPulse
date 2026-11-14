"""工作模式：工具循环里的任务完成态。

普通聊天不暴露重工具（``bash`` / ``file_ops`` / ``read_skill`` / ``workflow_state``，即声明了
``work_mode_only`` 的工具），模型要完成多步任务时先调用 ``enter_work_mode``。进入后：

- 重工具解锁，但模型自己的正文不再对外输出，只有工具结果和
  ``send_midway_msg`` 会真正发出去；
- 期间收到的群消息先暂存、不进上下文，只有点名当前人格时才把暂存一次性
  补进去。这样提示词前缀在整段工作期间保持不变，缓存命中不被破坏；
- 每次工作模式按会话记录完整轨迹（目标、每一轮正文与工具结果、结果），
  落到 ``memory/work_mode/sessions.json``，供 WebUI 查看。

自主回合与定时任务回合由框架自己走同一套工作模式：它们本来就是"她独自做事"
的时刻，重工具必须可用，过程也应该留在同一份轨迹里，所以不需要模型再调一次
``enter_work_mode``。

工作模式期间用哪个模型，取决于用哪个**任务名**——AMKR 按任务名换真实模型。因此
``memory/work_mode/settings.json`` 里的 ``task_name`` 留空表示沿用本回合原本的
任务名，填了则整段工作都用它（见 ``WorkModeStore.work_task_name``）。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sirius_pulse.core.prompt_factory import PromptFactory
from sirius_pulse.utils.json_io import atomic_write_json, read_json
from sirius_pulse.utils.layout import WorkspaceLayout

logger = logging.getLogger(__name__)

ENTER_WORK_MODE = "enter_work_mode"
QUIT_WORK_MODE = "quit_work_mode"
SEND_MIDWAY_MSG = "send_midway_msg"

#: 由 AgentLoop 直接处理的流程控制工具，不经过 ToolExecutor。
WORK_MODE_CONTROL_TOOL_NAMES = frozenset({ENTER_WORK_MODE, QUIT_WORK_MODE, SEND_MIDWAY_MSG})

#: 保留最近多少次工作模式轨迹（WebUI 只需要近期记录）。
MAX_RECORDED_SESSIONS = 50

#: 轨迹来源：模型自己进的工作模式，还是框架为自主/定时任务/后台工具回执回合自动开的。
WORK_MODE_SOURCE_CHAT = "chat"
WORK_MODE_SOURCE_AUTONOMY = "autonomy"
WORK_MODE_SOURCE_SCHEDULED = "scheduled"
WORK_MODE_SOURCE_PASSIVE_TOOL = "passive_tool"

_ENTER_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": ENTER_WORK_MODE,
        "description": (
            "多步的活（查资料、跑代码、处理文件）先进入工作模式，才能用 bash、file_ops 等重工具；"
            "只发一张图或一个文件不用进。这一轮的正文照常发到群里，先用一句话说你要做什么。"
            "进入后正文不再外发：过程要说的话用 send_midway_msg，做完或卡住就用 quit_work_mode 退出，"
            "它的 result 就是对外回复；工作不一定必须做完，卡住了如实说明卡点。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "要做成什么、怎样算做完。",
                }
            },
            "required": ["goal"],
        },
    },
}

_QUIT_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": QUIT_WORK_MODE,
        "description": (
            "做完了，或卡住需要别人帮忙时，退出工作模式回到聊天。"
            "result 会作为对外回复发出：说清做成了什么、没做成什么、卡在哪；没做成的不要说成做成了。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "result": {
                    "type": "string",
                    "description": "对外回复：做成了什么、没做成什么、卡在哪。",
                }
            },
            "required": ["result"],
        },
    },
}

_MIDWAY_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": SEND_MIDWAY_MSG,
        "description": (
            "工作模式里对群里说一句话：报个关键进展、确认一件事或提个问题，说完继续工作。"
            "中间过程和自己的思考不用发。"
        ),
        "parameters": {
            "type": "object",
            "properties": {"message": {"type": "string", "description": "要发到群里的话。"}},
            "required": ["message"],
        },
    },
}


def control_tools(*, active: bool) -> list[dict[str, Any]]:
    """本轮要额外提供给模型的流程控制工具。

    工作模式内提供退出与中途发言，工作模式外只提供进入。
    """
    schemas = (_QUIT_TOOL, _MIDWAY_TOOL) if active else (_ENTER_TOOL,)
    return [dict(schema) for schema in schemas]


def midway_message_tool() -> dict[str, Any]:
    """只提供「中途发言」这一个控制工具。

    框架自动开的回合（后台工具回调、定时任务、自主回合）不需要模型再调
    ``enter_work_mode`` / ``quit_work_mode``：回合本身就在工作模式里，不调工具
    就是结束。但回调回合的正文不外发，模型仍需要一个真正对外说话的入口，否则
    「画好啦」这类话只能烂在工作轨迹里。
    """
    return dict(_MIDWAY_TOOL)


def is_control_tool(name: str) -> bool:
    """Return whether a tool name is handled by the AgentLoop itself."""
    return (name or "").strip() in WORK_MODE_CONTROL_TOOL_NAMES


def parse_arguments(raw: str) -> dict[str, Any]:
    """Best-effort parse of a control tool's arguments."""
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class WorkModeRun:
    """一次进行中的工作模式：轨迹 + 进来但还没喂给模型的群消息。"""

    group_id: str
    goal: str
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    started_at: str = field(default_factory=_now_iso)
    status: str = "running"
    result: str = ""
    ended_at: str = ""
    source: str = WORK_MODE_SOURCE_CHAT
    task_name: str = ""
    """这次工作模式用的任务名（AMKR 按它换真实模型）；空 = 沿用本回合原本的任务名。"""
    steps: list[dict[str, Any]] = field(default_factory=list)
    stash: list[str] = field(default_factory=list)
    _model_stash: list[str] = field(default_factory=list, repr=False)
    replay: list[Any] = field(default_factory=list)
    """与 stash 一一对应的原始消息：退出时把还没进上下文的这批交回引擎重排。"""
    flush_pending: bool = False
    flushed_evidence_refs: list[str] = field(default_factory=list)
    store: Any = field(default=None, repr=False, compare=False)
    """这份轨迹的落盘视图；带上它，异常与取消路径也能把终态写下去。"""

    def stash_message(
        self,
        text: str,
        *,
        mentions_persona: bool,
        replay: Any = None,
    ) -> None:
        """Hold an inbound group message outside the model context.

        ``replay`` 是引擎给的原始消息载荷：这条消息如果一直没被点名、始终没进上下文，
        退出工作模式时要靠它重新排队，而不是直接丢掉。
        """
        text = (text or "").strip()
        if not text:
            return
        model_text = text
        if isinstance(replay, tuple) and replay:
            message = replay[0]
            model_text = PromptFactory.tag_message(
                str(getattr(message, "content", "") or text),
                speaker=str(getattr(message, "speaker", "") or ""),
                user_id=str(getattr(message, "channel_user_id", "") or ""),
                platform_message_id=str(getattr(message, "message_id", "") or ""),
                evidence_ref=str(getattr(message, "evidence_ref", "") or ""),
            )
        self.stash.append(text)
        self._model_stash.append(model_text)
        if replay is not None:
            self.replay.append(replay)
        if mentions_persona:
            self.flush_pending = True

    def take_flushed(self) -> list[str]:
        """Drain original plaintext while the model view is drained separately."""
        if not self.flush_pending:
            return []
        self.flush_pending = False
        # 只有实际补进本轮上下文的入站指令才能扩大来源集合。
        self.flushed_evidence_refs = list(
            dict.fromkeys(
                str(getattr(payload[0], "evidence_ref", "") or "")
                for payload in self.replay
                if isinstance(payload, tuple)
                and payload
                and getattr(payload[0], "evidence_ref", "")
            )
        )
        # 这批消息马上就会进上下文，不必也不能再排回队列。
        self.replay = []
        drained, self.stash = self.stash, []
        return drained

    def take_flushed_for_model(self) -> list[str]:
        """Drain the tagged view that preserves each admitted evidence_ref."""
        drained, self._model_stash = self._model_stash, []
        return drained

    def take_unanswered(self) -> list[Any]:
        """Drain the messages that never made it into the model context."""
        drained, self.replay = self.replay, []
        self.stash = []
        self._model_stash = []
        self.flush_pending = False
        return drained

    def add_step(self, **step: Any) -> None:
        """Append one round of the trajectory."""
        step.setdefault("round", len(self.steps) + 1)
        self.steps.append(step)

    def finish(self, *, result: str, status: str = "completed") -> None:
        """Close the run with its externally visible result."""
        self.result = str(result or "").strip()
        self.status = status
        self.ended_at = _now_iso()

    def finish_if_running(self, *, reason: str) -> bool:
        """收尾一次没有走到出口的工作，并立刻落盘。

        正常出口只有两条：``quit_work_mode`` 或轮次用尽。生成抛错、任务被取消、
        进程重启这些路径不会经过它们，于是轨迹会永远停在 ``running``——页面上
        看起来"还在干活"，实际上那段工作早就断了。返回是否真的收过尾。
        """
        if self.status != "running":
            return False
        self.finish(result=self.result or reason, status="aborted")
        if self.store is not None:
            self.store.save_run(self)
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "group_id": self.group_id,
            "goal": self.goal,
            "status": self.status,
            "result": self.result,
            "source": self.source,
            "task_name": self.task_name,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "steps": list(self.steps),
        }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """原子写盘，失败重试几次。

    轨迹与设置都是观察/配置数据，不能因为一次写盘失败打断正在进行的对话；
    Windows 上 ``os.replace`` 偶尔会被索引或杀软短暂占用，所以退避重试。
    """
    for attempt in range(3):
        try:
            atomic_write_json(path, payload)
            return
        except OSError:
            if attempt == 2:
                logger.warning("工作模式写入失败: %s", path, exc_info=True)
            else:
                time.sleep(0.05 * (attempt + 1))


class WorkModeStore:
    """``memory/work_mode/``：工作模式轨迹与设置的落盘视图。"""

    def __init__(self, work_path: Any) -> None:
        self.work_path = Path(work_path)

    @property
    def _dir(self) -> Path:
        return WorkspaceLayout(self.work_path).memory_dir() / "work_mode"

    @property
    def path(self) -> Path:
        return self._dir / "sessions.json"

    @property
    def settings_path(self) -> Path:
        return self._dir / "settings.json"

    def load(self) -> list[dict[str, Any]]:
        raw = read_json(self.path, None)
        items = raw.get("sessions") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)]

    def load_settings(self) -> dict[str, Any]:
        raw = read_json(self.settings_path, None)
        return raw if isinstance(raw, dict) else {}

    def work_task_name(self) -> str:
        """工作模式该用哪个任务名；空字符串表示沿用本回合原本的任务名。"""
        return str(self.load_settings().get("task_name", "") or "").strip()

    def reconcile_running(self, *, reason: str) -> int:
        """把上次进程留下的 ``running`` 轨迹标成未完成，返回改动条数。

        工作模式的运行态只活在内存里：进程一重启，写在盘上的 ``running`` 就一定是
        残留。不清理的话，页面上会一直挂着若干"进行中"的工作，看起来像还在干活。
        新进程只会调用它一次，因此这里不需要区分"是不是真的还在跑"。
        """
        sessions = self.load()
        changed = 0
        for session in sessions:
            if session.get("status") != "running":
                continue
            session["status"] = "aborted"
            session["ended_at"] = session.get("ended_at") or _now_iso()
            session["result"] = str(session.get("result") or "").strip() or reason
            changed += 1
        if changed:
            _write_json(self.path, {"sessions": sessions})
        return changed

    def save_settings(self, *, task_name: str) -> None:
        """记住工作模式使用的任务名；每次开始工作时重新读取，改完即刻生效。"""
        _write_json(self.settings_path, {"task_name": str(task_name or "").strip()})

    def save_run(self, run: WorkModeRun) -> None:
        """Upsert one run so a crash mid-work still leaves a readable trace.

        每次写入的都是这份会话的完整快照，所以单次写失败不会丢状态，下一轮会整份
        补上。
        """
        session = run.to_dict()
        session_id = run.session_id
        kept = [item for item in self.load() if str(item.get("session_id", "")) != session_id]
        kept.append(session)
        _write_json(self.path, {"sessions": kept[-MAX_RECORDED_SESSIONS:]})
