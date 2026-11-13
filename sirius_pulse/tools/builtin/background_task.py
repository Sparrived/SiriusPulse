"""查看与停止交给后台的任务：超时转入后台的 bash 命令、后台生图任务。

各类后台任务的状态仍由各自的工具维护（bash 的进程登记表、生图的持久化队列），
这里只是一个统一入口：按任务 ID 找到归属，再交给对应的实现去查看或停止。

权限：只能看到、停止当前会话发起的任务；developer 可以管理全部任务。
"""

from __future__ import annotations

from typing import Any

from sirius_pulse.tools.builtin import image_generate
from sirius_pulse.tools.builtin._internal import _bash_background
from sirius_pulse.tools.models import ToolInvocationContext

_STATUS_TOOL_OUTPUT_CHARS = 2_000

_STATUS_LABELS = {
    "running": "运行中",
    "pending": "排队中",
    "stopping": "正在停止",
    "exited": "已结束",
    "expired": "超过存活上限被结束",
    "stopped": "已停止",
    "done": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
}
_KIND_LABELS = {"bash": "bash", "image_generate": "生图"}
_ACTIVE_STATUSES = {"running", "pending", "stopping"}

TOOL_META = {
    "name": "background_task",
    "description": (
        "【适用场景】查看或停止你交给后台的任务：超时后转入后台的 bash 命令（bash_xxxxxxxx），"
        "以及 image_generate 提交的后台生图任务。\n"
        "【使用方法】action=list 列出当前会话仍在进行的后台任务；action=status 加 task_id "
        "查看单个任务（bash 任务附带最近的输出）；action=stop 加 task_id 停止任务。\n"
        "【注意事项】停止后不会再收到这个任务的结果回调；生图请求一旦已经发给生图服务，"
        "停止只是不再等结果，额度可能已经用掉。只能管理当前会话发起的任务。"
    ),
    "version": "1.0.0",
    "side_effect": "destructive",
    # 停止自己的后台任务不是对外投递，自主回合里也可以收拾自己开的摊子。
    "allowed_when_self_initiated": True,
    "tags": ["background", "task", "stop", "bash", "image"],
    "parameters": {
        "action": {
            "type": "str",
            "description": "list 列出进行中的任务；status 查看单个任务；stop 停止任务。",
            "required": True,
            "choices": ["list", "status", "stop"],
        },
        "task_id": {
            "type": "str",
            "description": "任务 ID：bash 转后台时返回的 bash_xxxxxxxx，或生图任务 ID。status/stop 必填。",
        },
    },
}


async def run(
    action: str,
    task_id: str = "",
    *,
    engine_context: Any = None,
    invocation_context: ToolInvocationContext | None = None,
    chat_context: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    if engine_context is None:
        return {"success": False, "error": "background_task 需要引擎上下文，当前不可用"}
    action = str(action or "").strip().lower()
    task_id = str(task_id or "").strip()
    scope = _Scope.of(invocation_context, chat_context)

    if action == "list":
        tasks = [task for task in _all_tasks(engine_context) if scope.allows(task)]
        active = [task for task in tasks if task["status"] in _ACTIVE_STATUSES]
        if not active:
            return _reply("现在没有在后台进行的任务。", {"tasks": []})
        lines = ["后台进行中的任务："] + [_one_line(task) for task in active]
        return _reply("\n".join(lines), {"tasks": active})

    if action not in {"status", "stop"}:
        return {"success": False, "error": f"不支持的 action: {action}（可用 list/status/stop）"}
    if not task_id:
        return {"success": False, "error": f"action={action} 需要 task_id"}

    found = _find_task(engine_context, task_id)
    if found is None or not scope.allows(found):
        return {
            "success": False,
            "error": f"找不到任务 {task_id}：可能不是当前会话发起的，或者已经被清理。",
        }

    if action == "status":
        text = _one_line(found)
        if found["kind"] == "bash":
            job = _bash_background.get(_bash_owner(engine_context), task_id)
            output = job.output_tail(_STATUS_TOOL_OUTPUT_CHARS) if job is not None else ""
            text += f"\n最近的输出:\n{output}" if output else "\n（还没有输出）"
        elif found.get("error"):
            text += f"\n原因：{found['error']}"
        return _reply(text, {"task": found})

    if found["kind"] == "bash":
        job = await _bash_background.stop(_bash_owner(engine_context), task_id)
        if job is None:
            return {"success": False, "error": f"找不到任务 {task_id}"}
        task = job.describe()
        if job.state == _bash_background.STOPPED and job.callback_state == "skipped":
            message = f"已停止后台命令 {task_id}。"
            tail = job.output_tail(_STATUS_TOOL_OUTPUT_CHARS)
            if tail:
                message += f"\n停止前的输出:\n{tail}"
        else:
            message = f"任务 {task_id} 已经结束（{_status_label(task['status'])}），无需停止。"
        return _reply(message, {"task": task, "stopped": job.state == _bash_background.STOPPED})

    stopped = await image_generate.stop_job(
        engine_context, engine_context.get_data_store("image_generate"), task_id
    )
    if stopped is None:
        return {"success": False, "error": f"找不到任务 {task_id}"}
    return _reply(
        f"生图任务 {task_id}：{stopped['message']}",
        {"task": stopped, "stopped": bool(stopped.get("stopped"))},
    )


class _Scope:
    def __init__(self, group_id: str, everything: bool) -> None:
        self.group_id = group_id
        self.everything = everything

    @classmethod
    def of(
        cls, invocation_context: ToolInvocationContext | None, chat_context: dict[str, Any] | None
    ) -> "_Scope":
        group_id = ""
        if invocation_context is not None:
            group_id = invocation_context.group_id
        if not group_id:
            group_id = str((chat_context or {}).get("group_id") or "")
        developer = bool(invocation_context is not None and invocation_context.caller_is_developer)
        return cls(group_id, developer)

    def allows(self, task: dict[str, Any]) -> bool:
        if self.everything:
            return True
        return bool(self.group_id) and task.get("group_id") == self.group_id


def _bash_owner(engine_context: Any) -> str:
    return _bash_background.owner_key(engine_context.get_data_store("bash"))


def _all_tasks(engine_context: Any) -> list[dict[str, Any]]:
    tasks = [job.describe() for job in _bash_background.list_jobs(_bash_owner(engine_context))]
    store = engine_context.get_data_store("image_generate")
    tasks.extend(image_generate.describe_job(job) for job in image_generate.list_jobs(store))
    return tasks


def _find_task(engine_context: Any, task_id: str) -> dict[str, Any] | None:
    if task_id.startswith(_bash_background.TASK_PREFIX):
        job = _bash_background.get(_bash_owner(engine_context), task_id)
        return job.describe() if job is not None else None
    store = engine_context.get_data_store("image_generate")
    for job in image_generate.list_jobs(store):
        if str(job.get("id") or "") == task_id:
            return image_generate.describe_job(job)
    return None


def _status_label(status: str) -> str:
    return _STATUS_LABELS.get(status, status or "未知")


def _one_line(task: dict[str, Any]) -> str:
    kind = _KIND_LABELS.get(task["kind"], task["kind"])
    status = _status_label(task["status"])
    if task["kind"] == "bash":
        if task["status"] == "running":
            status += f" {task['elapsed_seconds']:.0f} 秒"
        elif task.get("returncode") is not None:
            status += f"，退出码 {task['returncode']}"
        subject = task["command"].splitlines()[0][:120] if task["command"] else ""
    else:
        subject = task.get("prompt") or ""
    return f"- {task['task_id']}［{kind}·{status}］{subject}"


def _reply(text: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"success": True, "text_blocks": [text], "data": data}
