"""工作模式：工具循环里的任务完成态。

普通聊天不暴露重工具（``bash`` / ``read_skill`` / ``workflow_state`` /
``group_file_exec``），模型要完成多步任务时先调用 ``enter_work_mode``。进入后：

- 重工具解锁，但模型自己的正文不再对外输出，只有工具结果和
  ``send_midway_msg`` 会真正发出去；
- 期间收到的群消息先暂存、不进上下文，只有点名当前人格时才把暂存一次性
  补进去。这样提示词前缀在整段工作期间保持不变，缓存命中不被破坏；
- 每次工作模式按会话记录完整轨迹（目标、每一轮正文与工具结果、结果），
  落到 ``memory/work_mode/sessions.json``，供 WebUI 查看。
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

from sirius_pulse.utils.json_io import atomic_write_json, read_json
from sirius_pulse.utils.layout import WorkspaceLayout

logger = logging.getLogger(__name__)

#: 只在工作模式内可见的重工具。
WORK_MODE_ONLY_TOOL_NAMES = frozenset({"bash", "read_skill", "workflow_state", "group_file_exec"})

ENTER_WORK_MODE = "enter_work_mode"
QUIT_WORK_MODE = "quit_work_mode"
SEND_MIDWAY_MSG = "send_midway_msg"

#: 由 AgentLoop 直接处理的流程控制工具，不经过 ToolExecutor。
WORK_MODE_CONTROL_TOOL_NAMES = frozenset({ENTER_WORK_MODE, QUIT_WORK_MODE, SEND_MIDWAY_MSG})

#: 保留最近多少次工作模式轨迹（WebUI 只需要近期记录）。
MAX_RECORDED_SESSIONS = 50

_ENTER_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": ENTER_WORK_MODE,
        "description": (
            "进入工作模式去完成一个需要多步工具协作的任务。"
            "只有工作模式里才能使用 bash、read_skill、workflow_state、group_file_exec。"
            "进入后你输出的正文不会发给任何人，只有工具结果和 send_midway_msg 有效。"
            "任务完成、或者卡住需要外部帮助时，用 quit_work_mode 退出。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "这次准备干什么：一句话说明目标和完成标准。",
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
        "description": ("退出工作模式，返回普通聊天。result 会作为对外回复发送。" "任务没做完也可以退出：说明卡在哪里、需要外界提供什么，好向群里求助。"),
        "parameters": {
            "type": "object",
            "properties": {
                "result": {
                    "type": "string",
                    "description": "这次工作的结果：做完了什么、没做完什么、需要什么帮助。",
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
        "description": ("在工作模式里向群里发一条消息。" "遇到重要里程碑、需要公开确认、或者有疑问要向外界提问时使用；" "问完可以继续工作，不需要退出工作模式。"),
        "parameters": {
            "type": "object",
            "properties": {"message": {"type": "string", "description": "要发给群里的内容。"}},
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
    steps: list[dict[str, Any]] = field(default_factory=list)
    stash: list[str] = field(default_factory=list)
    flush_pending: bool = False

    def stash_message(self, text: str, *, mentions_persona: bool) -> None:
        """Hold an inbound group message outside the model context."""
        text = (text or "").strip()
        if not text:
            return
        self.stash.append(text)
        if mentions_persona:
            self.flush_pending = True

    def take_flushed(self) -> list[str]:
        """Drain the stash, but only once a message named the persona."""
        if not self.flush_pending:
            return []
        self.flush_pending = False
        drained, self.stash = self.stash, []
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "group_id": self.group_id,
            "goal": self.goal,
            "status": self.status,
            "result": self.result,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "steps": list(self.steps),
        }


class WorkModeStore:
    """``memory/work_mode/sessions.json``：工作模式轨迹的落盘视图。"""

    def __init__(self, work_path: Any) -> None:
        self.work_path = Path(work_path)

    @property
    def path(self) -> Path:
        return WorkspaceLayout(self.work_path).memory_dir() / "work_mode" / "sessions.json"

    def load(self) -> list[dict[str, Any]]:
        raw = read_json(self.path, None)
        items = raw.get("sessions") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)]

    def save_run(self, run: WorkModeRun) -> None:
        """Upsert one run so a crash mid-work still leaves a readable trace.

        每次写入的都是这份会话的完整快照，所以单次写失败不会丢状态，下一轮会整份
        补上；轨迹是观察数据，不能因为写盘失败打断正在进行的对话。重试是因为
        Windows 上 ``os.replace`` 偶尔会被索引或杀软短暂占用。
        """
        session = run.to_dict()
        session_id = run.session_id
        kept = [item for item in self.load() if str(item.get("session_id", "")) != session_id]
        kept.append(session)
        payload = {"sessions": kept[-MAX_RECORDED_SESSIONS:]}
        for attempt in range(3):
            try:
                atomic_write_json(self.path, payload)
                return
            except OSError:
                if attempt == 2:
                    logger.warning("工作模式轨迹写入失败: %s", self.path, exc_info=True)
                else:
                    time.sleep(0.05 * (attempt + 1))
