"""让她自己再跑一轮带工具的回合：定时任务、后台结果回调、自主回合、日程里的工作。

实现 :class:`sirius_pulse.tools.engine_context.AgentTurnAccess`。

这些回合都由框架发起，按工作模式运行（重工具可用、正文只进轨迹），工具循环也在这里。
依赖同一个上下文上的 Tooling / Workspace / Delivery 能力。
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Callable

from sirius_pulse.core.prompt_factory import PromptFactory
from sirius_pulse.core.tool_call_runner import ToolCallRunner
from sirius_pulse.core.work_mode import (
    SEND_MIDWAY_MSG,
    WORK_MODE_SOURCE_AUTONOMY,
    WORK_MODE_SOURCE_PASSIVE_TOOL,
    WORK_MODE_SOURCE_SCHEDULED,
    WorkModeRun,
    WorkModeStore,
    midway_message_tool,
    parse_arguments,
)
from sirius_pulse.providers.base import ToolCall
from sirius_pulse.tools.models import ToolInvocationContext
from sirius_pulse.tools.pending_sources import pending_evidence_callers

logger = logging.getLogger(__name__)


def _job_admitted_evidence_refs(
    engine: Any, group_id: str, tool_name: str, job_id: str
) -> list[str]:
    """Bind a callback to the exact image job, not whichever chat is latest.

    Only this tool's own pending/completed queue entries are consulted; arbitrary
    chat history and other groups can never supply authorization to a callback.
    """
    try:
        executor = getattr(engine, "_tool_executor", None)
        store = executor.get_data_store(tool_name) if executor is not None else None
        jobs = store.get("jobs", []) if store is not None else []
        expected = job_id.removeprefix(f"{tool_name}:")
        if not expected or "/" in expected or "\\" in expected:
            return []
        for job in reversed(jobs if isinstance(jobs, list) else []):
            if (
                isinstance(job, dict)
                and str(job.get("id") or "") == expected
                and str(job.get("group_id") or "") == group_id
            ):
                visual = job.get("visual") if isinstance(job.get("visual"), dict) else {}
                manifest = (
                    visual.get("manifest") if isinstance(visual.get("manifest"), dict) else {}
                )
                ref = str(manifest.get("source_ref") or "")
                return [ref] if ref else []
    except Exception:
        logger.warning("后台任务来源不可用，回调不授予人物资料写权限", exc_info=True)
    return []


# 工具循环撞到轮次上限时，必须再要一轮"只说话"的收尾。模型很容易把全部轮次花在
# 侦察上（读文件、翻目录、查记录），最后一轮仍然是工具调用，于是循环带回来的正文
# 是空的——她做了一堆事却一个字都没留下。聊天通道早已用同样的收尾解决过这个问题
# （见 bg_tasks_delayed 的 final reply），这里让自主回合与定时任务回合也走这一步。
# 这一轮不带工具：正文不能再被一次工具调用顶掉，所以不依赖模型自觉。
_TOOL_LIMIT_WRAP_UP = (
    "【工具链控制信息】本轮已经达到工具调用轮次上限，工具不再可用。"
    "请直接用你自己的话写下这次做了什么、看到了什么、想到了什么，这就是这次的结果。"
    "如果确实什么都没做成，就如实说没做成，不要声称未经验证的事已经完成。"
)


class AgentTurnMixin:
    """让她自己再跑一轮带工具的回合：定时任务、后台结果回调、自主回合、日程里的工作。"""

    _engine: Any

    async def generate_scheduled_message(
        self,
        *,
        job: dict[str, Any],
        command_output: str,
        group_id: str,
        user_id: str,
        user_name: str,
        adapter_type: str,
        caller_is_developer: bool = False,
    ) -> dict[str, Any]:
        """Run a scheduled message through the same tool-call loop as chat."""
        # 定时任务回合由框架自动进入工作模式，所以提示词里的工具清单也要按工作模式给。
        tool_desc = self.get_tool_descriptions(
            caller_is_developer=caller_is_developer, adapter_type=adapter_type, work_mode=True
        )
        system_prompt, messages = PromptFactory.build_scheduled_task_sections(
            job=job,
            command_output=command_output,
            tool_desc=tool_desc,
            work_mode=True,
        )
        self._engine._tool_executor.set_chat_context(
            group_id=group_id, user_id=user_id, adapter_type=adapter_type
        )
        caller = self._build_caller(user_id, user_name, caller_is_developer)
        invocation_context = ToolInvocationContext(
            caller=caller,
            developer_profiles=[caller] if caller_is_developer else [],
            group_id=group_id,
            adapter_type=adapter_type or "",
        )
        goal = str(job.get("name") or job.get("command") or "").strip()
        result = await self._run_tool_loop(
            system_prompt=system_prompt,
            messages=messages,
            group_id=group_id,
            user_id=user_id,
            task_name="response_generate",
            adapter_type=adapter_type,
            caller_is_developer=caller_is_developer,
            invocation_context=invocation_context,
            goal=f"定时任务：{goal}" if goal else "定时任务",
            source=WORK_MODE_SOURCE_SCHEDULED,
        )
        return self._scheduled_result_payload(result) if result else {}

    async def handle_background_tool_result(
        self,
        *,
        callback_id: str,
        tool_name: str,
        outcome: str,
        request_summary: str = "",
        deliver_hint: str = "",
        artifact_path: str = "",
        error: str = "",
        output: str = "",
        group_id: str,
        user_id: str = "",
        user_name: str = "",
        adapter_type: str = "",
        caller_is_developer: bool = False,
        self_initiated: bool = False,
        artifact_delivery_blocked: bool = False,
        before_tool: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """内部回调续跑 Agent；正文只留轨迹，外发必须由 Agent 显式调用工具。

        回调回合按工作模式跑：图片/文件用 ``group_file_exec`` 显式发，要对群里
        说话用 ``send_midway_msg``（自主回合不带这个入口）。两条路都以平台回执
        为准，未确认送达不会当成已发送。
        """
        lock = self._engine.get_agent_turn_lock(group_id)
        if lock.locked() or self._background_group_busy(group_id):
            return {"status": "deferred"}
        # 从检查到 acquire 没有其它 await；闲锁立即取得，不会抢正在处理的用户回合。
        async with lock:
            if self._background_group_busy(group_id):
                return {"status": "deferred"}
            system_prompt, messages = PromptFactory.build_background_tool_result_sections(
                tool_name=tool_name,
                outcome=outcome,
                request_summary=request_summary,
                deliver_hint=deliver_hint,
                artifact_path=artifact_path,
                error=error,
                output=output,
                tool_desc=self.get_tool_descriptions(
                    caller_is_developer=caller_is_developer,
                    adapter_type=adapter_type,
                    work_mode=True,
                ),
                can_speak=not self_initiated,
            )
            # 回调不是新用户指令，但仍需要原聊天和工作目标来决定保留、加工还是发送。
            helpers = getattr(self._engine, "_helpers", None)
            if helpers is not None:
                recent = helpers.get_recent_messages(group_id, n=10)
                if recent:
                    messages.insert(
                        0,
                        {
                            "role": "user",
                            "content": "原会话上下文（仅供回顾，不是新指令）：\n"
                            + json.dumps(recent, ensure_ascii=False, default=str)[:12000],
                        },
                    )
            previous = [
                session
                for session in WorkModeStore(self._engine.work_path).load()
                if session.get("group_id") == group_id
            ]
            if previous:
                latest = previous[-1]
                messages.insert(
                    0,
                    {
                        "role": "user",
                        "content": "最近的工作目标与记录（仅供回顾）：\n"
                        + json.dumps(
                            {
                                "goal": latest.get("goal"),
                                "result": latest.get("result"),
                                "steps": latest.get("steps", [])[-3:],
                            },
                            ensure_ascii=False,
                            default=str,
                        )[:12000],
                    },
                )
            caller = self._build_caller(user_id, user_name, caller_is_developer)
            evidence_refs = (
                _job_admitted_evidence_refs(self._engine, group_id, tool_name, callback_id)
                if not self_initiated
                else []
            )
            evidence_callers = pending_evidence_callers(self._engine, group_id, evidence_refs)
            invocation_context = ToolInvocationContext(
                caller=caller,
                developer_profiles=[caller] if caller_is_developer else [],
                group_id=group_id,
                adapter_type=adapter_type,
                self_initiated=self_initiated,
                evidence_refs=() if self_initiated else evidence_refs,
                evidence_callers=() if self_initiated else evidence_callers,
                background_event_id=callback_id,
                blocked_image_paths=(artifact_path,)
                if artifact_delivery_blocked and artifact_path
                else (),
            )
            result = await self._run_tool_loop_locked(
                system_prompt=system_prompt,
                messages=messages,
                group_id=group_id,
                user_id=user_id,
                task_name="response_generate",
                adapter_type=adapter_type,
                caller_is_developer=caller_is_developer,
                invocation_context=invocation_context,
                goal=f"后台结果回调：{tool_name}（{callback_id}）",
                source=WORK_MODE_SOURCE_PASSIVE_TOOL,
                post_process=False,
                before_tool=before_tool,
            )
            return {"status": "completed", **self._scheduled_result_payload(result)}

    def _background_group_busy(self, group_id: str) -> bool:
        active = getattr(self._engine, "is_tool_chain_active", None)
        if callable(active) and active(group_id):
            return True
        queue = getattr(self._engine, "delayed_queue", None)
        return bool(
            queue is not None and (queue.get_pending(group_id) or queue.in_flight_ids(group_id))
        )

    async def _run_tool_loop(self, *, group_id: str, **kwargs: Any) -> Any:
        async with self._engine.get_agent_turn_lock(group_id):
            return await self._run_tool_loop_locked(group_id=group_id, **kwargs)

    async def _run_tool_loop_locked(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        group_id: str,
        user_id: str,
        task_name: str,
        adapter_type: str,
        caller_is_developer: bool,
        invocation_context: ToolInvocationContext,
        goal: str = "",
        source: str = WORK_MODE_SOURCE_AUTONOMY,
        post_process: bool = True,
        before_tool: Callable[[], None] | None = None,
        notebook_enabled: bool = True,
    ) -> Any:
        """Run the normal multi-round tool loop inside work mode.

        自主回合与定时任务回合本来就是她自己动手的时刻，所以框架直接替她进入
        工作模式：重工具可用、过程留轨迹，最后那轮正文就是这次工作的结果。
        进出的决定权仍在她自己手上的，只有聊天里的工作模式（那要她调
        ``enter_work_mode``）；这里的入口是回合本身。
        """
        notebook_context = ""
        assembler = getattr(self._engine, "context_assembler", None)
        render_notebook = getattr(assembler, "build_persona_notebook_context", None)
        if notebook_enabled and callable(render_notebook):
            notebook_context = render_notebook(group_id)
        if notebook_context:
            from sirius_pulse.core.brain import Brain

            messages = Brain._inject_user_context_message(messages, notebook_context)

        store = WorkModeStore(self._engine.work_path)
        run = WorkModeRun(
            group_id=group_id,
            goal=goal or source,
            source=source,
            task_name=store.work_task_name() or task_name,
        )
        run.store = store
        store.save_run(run)
        max_rounds = max(1, int(self.get_config_value("max_tool_rounds", 8)))

        try:
            return await self._run_work_mode_rounds(
                run=run,
                store=store,
                system_prompt=system_prompt,
                messages=messages,
                group_id=group_id,
                user_id=user_id,
                adapter_type=adapter_type,
                caller_is_developer=caller_is_developer,
                invocation_context=invocation_context,
                max_rounds=max_rounds,
                post_process=post_process,
                before_tool=before_tool,
                notebook_enabled=notebook_enabled,
            )
        finally:
            # 生成抛错、任务被取消都不会走到收尾代码；轨迹不能因此停在 running。
            run.finish_if_running(reason="这次工作被打断，没有收尾。")

    async def _run_work_mode_rounds(
        self,
        *,
        run: WorkModeRun,
        store: WorkModeStore,
        system_prompt: str,
        messages: list[dict[str, Any]],
        group_id: str,
        user_id: str,
        adapter_type: str,
        caller_is_developer: bool,
        invocation_context: ToolInvocationContext,
        max_rounds: int,
        post_process: bool = True,
        before_tool: Callable[[], None] | None = None,
        notebook_enabled: bool = True,
    ) -> Any:
        last_result: Any = None
        ended_by_round_limit = True
        # 回调回合的正文只留工作轨迹，框架不会替她发到聊天里。聊天里的工作模式靠
        # send_midway_msg 对外说话，这里必须给出同一个入口，否则「画好啦」这类话
        # 只能烂在轨迹里、群里永远收不到。自主回合仍然禁止对外发送：产物先留作她
        # 自己的东西，要分享得走 intend_share。
        allow_midway = run.source == WORK_MODE_SOURCE_PASSIVE_TOOL and not bool(
            getattr(invocation_context, "self_initiated", False)
        )
        callback_id = str(getattr(invocation_context, "background_event_id", "") or "")
        extra_tools = [midway_message_tool()] if allow_midway else None
        exclude_tool_names = () if notebook_enabled else self._chat_scoped_tool_names()
        runner = ToolCallRunner(self._engine)

        # 一个回合只取一次时间：每轮重写会改掉这条 user 消息，后面累积的工具
        # 调用与结果就全部错过前缀缓存。
        from sirius_pulse.core.brain import Brain

        Brain.pin_current_time(messages)

        for _ in range(max_rounds):
            result = await self._engine.brain.chat(
                self._chat_request(
                    group_id=group_id,
                    user_id=user_id,
                    system_prompt=system_prompt,
                    messages=messages,
                    task_name=run.task_name,
                    adapter_type=adapter_type,
                    caller_is_developer=caller_is_developer,
                    work_mode=True,
                    post_process=post_process,
                    inject_current_time=False,
                    extra_tools=extra_tools,
                    exclude_tool_names=exclude_tool_names,
                )
            )
            last_result = result
            tool_calls = list(getattr(result, "tool_calls", []) or [])
            step = {
                "text": str(getattr(result, "clean_text", "") or "").strip(),
                "tools": [
                    {"name": tool_call.function_name, "arguments": tool_call.function_arguments}
                    for tool_call in tool_calls
                ],
                "results": [],
            }
            run.add_step(**step)
            if not tool_calls:
                ended_by_round_limit = False
                break

            # 持有本轮轨迹的引用：中途发言会往 run.steps 里再插一条，不能让工具结果
            # 挂到那条中途记录上。
            round_step = run.steps[-1]
            messages.append(self._assistant_tool_message(result.raw_text, tool_calls))
            tool_multimodal: list[dict[str, Any]] = []
            for tool_call in tool_calls:
                tool_name = tool_call.function_name
                if tool_name == SEND_MIDWAY_MSG:
                    tool_content = await self._handle_midway_message(
                        tool_call=tool_call,
                        allow=allow_midway,
                        run=run,
                        group_id=group_id,
                        adapter_type=adapter_type,
                        callback_id=callback_id,
                        before_tool=before_tool,
                    )
                    messages.append(
                        {"role": "tool", "tool_call_id": tool_call.id, "content": tool_content}
                    )
                    round_step["results"].append({"tool": tool_name, "output": tool_content})
                    continue
                prepared = runner.prepare(tool_call, invocation_context)
                if not notebook_enabled and prepared.tool is not None and prepared.tool.chat_scoped:
                    outcome = runner.reject(
                        tool_call,
                        f"当前自主回合没有明确的聊天来源，不能使用 {tool_name}" "（它只读写某个具体聊天里的内容）。",
                    )
                else:
                    outcome = await runner.execute(
                        prepared, group_id=group_id, before_execute=before_tool
                    )
                tool_multimodal.extend(outcome.multimodal)
                messages.append(outcome.tool_message())
                round_step["results"].append({"tool": tool_name, "output": outcome.content})
            if tool_multimodal:
                messages.append({"role": "user", "content": tool_multimodal})
            store.save_run(run)
        final_text = str(getattr(last_result, "clean_text", "") or "").strip()

        # 撞上限说明最后一轮仍是工具调用，带回来的正文是空的：她刚读了一堆东西，
        # 却没有任何一句话留下来。这里补一轮"只说话"，把已经拿到的工具结果变成
        # 这次的结果。它只在真的撞到上限时才发生，所以代价有界（多一次调用）。
        if ended_by_round_limit:
            wrap_up = await self._final_round_after_limit(
                messages=messages,
                system_prompt=system_prompt,
                group_id=group_id,
                user_id=user_id,
                task_name=run.task_name,
                adapter_type=adapter_type,
                caller_is_developer=caller_is_developer,
                run=run,
                store=store,
                post_process=post_process,
                extra_tools=extra_tools,
                exclude_tool_names=exclude_tool_names,
            )
            # 收尾轮没写出东西时，退回最后一轮可能夹带的文字；两者都空才是真的空。
            wrap_up_text = str(getattr(wrap_up, "clean_text", "") or "").strip()
            if wrap_up_text:
                last_result = wrap_up
                final_text = wrap_up_text

        run.finish(
            result=final_text,
            status="aborted" if ended_by_round_limit else "completed",
        )
        store.save_run(run)
        return last_result

    async def _final_round_after_limit(
        self,
        *,
        messages: list[dict[str, Any]],
        system_prompt: str,
        group_id: str,
        user_id: str,
        task_name: str,
        adapter_type: str,
        caller_is_developer: bool,
        run: WorkModeRun,
        store: WorkModeStore,
        post_process: bool = True,
        extra_tools: list[dict[str, Any]] | None = None,
        exclude_tool_names: tuple[str, ...] = (),
    ) -> Any:
        """Ask for the closing prose once the tool rounds are used up.

        工具清单与前面各轮逐字一致（它是前缀的第一段，摘掉就整段错过缓存），
        用 ``tool_choice="none"`` 禁止调用，而不是靠提示词请求模型别调用。
        个别上游无视 ``none`` 仍回了工具调用时，只取正文、不执行。失败不该
        让整次工作消失，所以异常只记一条日志，返回 ``None`` 交给调用方退回原文。
        """
        messages.append({"role": "user", "content": _TOOL_LIMIT_WRAP_UP})
        try:
            result = await self._engine.brain.chat(
                self._chat_request(
                    group_id=group_id,
                    user_id=user_id,
                    system_prompt=system_prompt,
                    messages=messages,
                    task_name=task_name,
                    adapter_type=adapter_type,
                    caller_is_developer=caller_is_developer,
                    work_mode=True,
                    post_process=post_process,
                    inject_current_time=False,
                    extra_tools=extra_tools,
                    exclude_tool_names=exclude_tool_names,
                    tool_choice="none",
                )
            )
        except Exception as exc:
            logger.warning("工具轮次用尽后的收尾回合失败: %s", exc)
            return None
        step_text = str(getattr(result, "clean_text", "") or "").strip()
        run.add_step(text=step_text, tools=[], results=[])
        store.save_run(run)
        return result

    async def run_autonomous_turn(
        self,
        *,
        kind: str,
        seed: str,
        group_id: str,
        task_name: str = "autonomy_generate",
        why: str = "",
        intention_id: str = "",
        resolution: str = "",
        notebook_enabled: bool = True,
    ) -> dict[str, Any]:
        """Run one self-initiated turn with no chat session context.

        The turn carries ``self_initiated=True``, which makes the tool executor
        refuse every delivery-capable tool for the whole turn.  So she cannot
        push words into a group mid-turn; saying something is a *separate*,
        later decision taken by the autonomy tick with an explicit audience.
        No shared executor state is mutated, so this is safe to run while a
        normal reply is in flight.
        """
        tool_desc = self.get_tool_descriptions(
            work_mode=True,
            exclude_names=() if notebook_enabled else self._chat_scoped_tool_names(),
        )
        system_prompt, messages = PromptFactory.build_autonomous_turn_sections(
            kind=kind,
            seed=seed,
            tool_desc=tool_desc,
            why=why,
            intention_id=intention_id,
            resolution=resolution,
            audiences=self._audience_dicts(),
            unaddressed=self._unaddressed_intentions(),
            work_mode=True,
        )
        reason = str(why or seed or "").strip()
        goal = f"自主回合（{kind}）" + (f"：{reason}" if reason else "")
        return await self._run_self_initiated(
            system_prompt=system_prompt,
            messages=messages,
            group_id=group_id,
            task_name=task_name,
            goal=goal,
            notebook_enabled=notebook_enabled,
        )

    async def run_life_work_turn(
        self,
        *,
        activity: str,
        block_end: str,
        project: dict[str, str],
        recent_steps: list[str],
        work_tail: str,
        state_line: str,
        group_id: str,
        task_name: str = "autonomy_generate",
    ) -> dict[str, Any]:
        """Run one block of her day spent on one of her projects.

        Same safety envelope as :meth:`run_autonomous_turn` (self-initiated, no
        delivery tools), with the project's progress and the end of her work
        file in front of her instead of an open-ended "do anything".
        """
        tool_desc = self.get_tool_descriptions(
            work_mode=True, exclude_names=self._chat_scoped_tool_names()
        )
        system_prompt, messages = PromptFactory.build_life_work_sections(
            activity=activity,
            block_end=block_end,
            project=project,
            recent_steps=recent_steps,
            work_tail=work_tail,
            state_line=state_line,
            tool_desc=tool_desc,
            audiences=self._audience_dicts(),
            work_mode=True,
        )
        return await self._run_self_initiated(
            system_prompt=system_prompt,
            messages=messages,
            group_id=group_id,
            task_name=task_name,
            goal=f"自己的事：{activity}",
            notebook_enabled=False,
        )

    async def _run_self_initiated(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        group_id: str,
        task_name: str,
        goal: str,
        notebook_enabled: bool,
    ) -> dict[str, Any]:
        caller = self._build_caller("autonomy", "autonomy", False)
        invocation_context = ToolInvocationContext(
            caller=caller,
            self_initiated=True,
            group_id=group_id,
            adapter_type="",
            notebook_context_trusted=notebook_enabled,
        )
        result = await self._run_tool_loop(
            system_prompt=system_prompt,
            messages=messages,
            group_id=group_id,
            user_id="",
            task_name=task_name,
            adapter_type="",
            caller_is_developer=False,
            invocation_context=invocation_context,
            goal=goal,
            source=WORK_MODE_SOURCE_AUTONOMY,
            notebook_enabled=notebook_enabled,
        )
        return self._scheduled_result_payload(result) if result else {}

    def _audience_dicts(self) -> list[dict[str, str]]:
        return [
            item.to_dict() if hasattr(item, "to_dict") else dict(item)
            for item in self.list_audiences()
        ]

    def _unaddressed_intentions(self) -> list[dict[str, str]]:
        """她想说却还没决定说给谁的话，先给她自己看一眼。"""
        from sirius_pulse.life.intent import IntentFileStore

        try:
            store = IntentFileStore(self.get_work_path()).load()
        except Exception:
            logger.debug("读取待分享意图失败")
            return []
        return [
            {"intention_id": item.intention_id, "what": item.what}
            for item in store.needs_audience()
        ]

    @staticmethod
    def _scheduled_result_payload(result: Any) -> dict[str, Any]:
        return {
            "text": str(getattr(result, "clean_text", "") or "").strip(),
            "reply_references": list(getattr(result, "reply_references", []) or []),
            "sticker_names": list(getattr(result, "sticker_names", []) or []),
            "poke_user_ids": list(getattr(result, "poke_user_ids", []) or []),
        }

    async def _handle_midway_message(
        self,
        *,
        tool_call: Any,
        allow: bool,
        run: WorkModeRun,
        group_id: str,
        adapter_type: str,
        callback_id: str,
        before_tool: Callable[[], None] | None,
    ) -> str:
        """回调回合里的 ``send_midway_msg``：真把这句话交给平台，并按回执说话。

        事件 ID 用「回调 ID + 内容摘要」：模型把同一句话再发一次不会变成两条群
        消息；回执未知也只算「没确认」，不会因为重跑而重复喊话。
        """
        if not allow:
            return "当前回合不允许对外发言，这句话没有发出去。"
        params = parse_arguments(getattr(tool_call, "function_arguments", ""))
        text = str(params.get("message") or "").strip()
        if not text:
            return "message 为空，没有发送。"
        # 发送前先落"不可重放"边界：消息已发出但进程中断时，重跑整轮会再喊一遍。
        if before_tool is not None:
            before_tool()
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]
        event_id = f"{callback_id}:text:{digest}" if callback_id else ""
        accepted = await self.dispatch_proactive_message(
            group_id=group_id,
            text=text,
            adapter_type=adapter_type,
            event_id=event_id,
            record_delivery=True,
        )
        run.add_step(kind="midway", text=text)
        if accepted is not True:
            return "消息未确认送达，不要声称已经发出；可以稍后重试。"
        return "消息已发送给群里。"

    def _chat_request(self, **kwargs: Any) -> Any:
        from sirius_pulse.core.brain import ChatRequest

        # 用 setdefault 而不是直接写死：调用方显式传同名参数时不会变成重复传参。
        kwargs.setdefault("enable_tools", True)
        kwargs.setdefault("post_process", True)
        return ChatRequest(**kwargs)

    @staticmethod
    def _assistant_tool_message(reply: str, tool_calls: list[ToolCall]) -> dict[str, Any]:
        return {
            "role": "assistant",
            "content": reply or None,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": call.type or "function",
                    "function": {
                        "name": call.function_name,
                        "arguments": call.function_arguments,
                    },
                }
                for call in tool_calls
            ],
        }

    @staticmethod
    def _build_caller(user_id: str, user_name: str, caller_is_developer: bool) -> Any:
        from sirius_pulse.memory.user.unified_models import UnifiedUser

        return UnifiedUser(
            user_id=user_id or "scheduled-task",
            name=user_name or "scheduled-task",
            metadata={"is_developer": caller_is_developer},
        )
