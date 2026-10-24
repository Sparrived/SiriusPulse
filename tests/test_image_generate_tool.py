"""生图只落盘并把结果交回 Agent；完成回调不等于对外送达。

回调可静默、延期或继续工作。任何工具动作前持久化 unsafe，崩溃后不得重跑副作用。
真实 Engine/Brain、工具和平台回执链见 flow 测试。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from sirius_pulse.memory.user.unified_models import UnifiedUser
from sirius_pulse.tools.builtin import image_generate
from sirius_pulse.tools.data_store import ToolDataStore
from sirius_pulse.tools.models import ToolInvocationContext

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-png-body"
_GROUP_CONTEXT = {"group_id": "987654321", "user_id": "u-1", "adapter_type": "napcat"}


def _store(tmp_path: Path) -> ToolDataStore:
    return ToolDataStore(tmp_path / "tool_data" / "image_generate.json")


def _jobs(store: ToolDataStore) -> list[dict[str, Any]]:
    return ToolDataStore(store.store_path).get("jobs", [])


class _FakeCtx:
    def __init__(
        self,
        *,
        image_error=None,
        callback_error=None,
        unsafe=False,
        callback_results=None,
    ):
        self.image_error = image_error
        self.callback_error = callback_error
        self.unsafe = unsafe
        self.callback_results = list(callback_results or [])
        self.image_calls = []
        self.callback_calls = []
        self.dispatch_calls = []
        self.on_callback = None

    async def generate_image(self, *, prompt, reference_images=None, timeout_seconds=300.0):
        self.image_calls.append(
            {
                "prompt": prompt,
                "reference_images": reference_images,
                "timeout_seconds": timeout_seconds,
            }
        )
        if self.image_error is not None:
            raise self.image_error
        return {"image_bytes": _PNG_BYTES, "mime_type": "image/png"}

    async def handle_background_tool_result(self, **kwargs):
        self.callback_calls.append(kwargs)
        if self.unsafe:
            kwargs["before_tool"]()
        if self.on_callback is not None:
            self.on_callback(kwargs)
        if self.callback_error is not None:
            raise self.callback_error
        return (
            self.callback_results.pop(0)
            if self.callback_results
            else {
                "status": "completed",
                "text": "",
            }
        )

    async def dispatch_proactive_message(self, **kwargs):
        self.dispatch_calls.append(kwargs)
        raise AssertionError("image_generate must not automatically publish a result")


class _CtxWithStore(_FakeCtx):
    def __init__(self, store, **kwargs):
        super().__init__(**kwargs)
        self.store = store

    def get_data_store(self, name):
        assert name == "image_generate"
        return self.store


def test_background_task_spec_polls_the_queue(tmp_path):
    specs = image_generate.create_background_tasks(_CtxWithStore(_store(tmp_path)))
    assert len(specs) == 1
    assert specs[0].name == "image_generate_jobs"
    assert specs[0].interval_seconds == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_submit_is_persistent_and_does_not_promise_automatic_publication(tmp_path):
    store = _store(tmp_path)
    result = await image_generate.run(
        prompt="画三种猫的方案",
        data_store=store,
        chat_context=_GROUP_CONTEXT,
    )
    assert result["success"] is True
    assert "已提交" in "\n".join(result["text_blocks"])
    assert "自动投递图片" not in "\n".join(result["text_blocks"])
    job = _jobs(store)[0]
    assert job["status"] == "pending"
    assert job["callback_state"] == "pending"
    assert job["notified"] is False
    assert job["prompt"] == "画三种猫的方案"
    assert job["id"].startswith("img_")
    assert job["group_id"] == _GROUP_CONTEXT["group_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("summary", ["", "先留着这张，等三张齐了再选。"])
async def test_batch_artifacts_only_return_to_agent_even_when_callback_has_text(tmp_path, summary):
    store = _store(tmp_path)
    for number in range(3):
        result = await image_generate.run(
            prompt=f"猫的方案{number}",
            data_store=store,
            chat_context=_GROUP_CONTEXT,
        )
        assert result["success"] is True
    ctx = _FakeCtx(callback_results=[{"status": "completed", "text": summary}] * 3)
    for _ in range(4):
        await image_generate._process_once(ctx, store)

    jobs = _jobs(store)
    assert len(jobs) == 3
    assert all(job["callback_state"] == "completed" and job["notified"] for job in jobs)
    assert all(Path(job["image_path"]).read_bytes() == _PNG_BYTES for job in jobs)
    assert len(ctx.image_calls) == len(ctx.callback_calls) == 3
    assert ctx.dispatch_calls == []
    for job, callback in zip(jobs, ctx.callback_calls):
        assert callback["callback_id"] == f"image_generate:{job['id']}"
        assert callback["artifact_path"] == job["image_path"]
        assert callback["outcome"] == "success"
        assert callback["request_summary"] == job["prompt"]
        assert callable(callback["before_tool"])

    await image_generate._process_once(ctx, ToolDataStore(store.store_path))
    assert len(ctx.image_calls) == len(ctx.callback_calls) == 3
    assert ctx.dispatch_calls == []


@pytest.mark.asyncio
async def test_generation_failure_only_returns_honest_error_to_agent(tmp_path):
    store = _store(tmp_path)
    await image_generate.run(prompt="画一张图", data_store=store, chat_context=_GROUP_CONTEXT)
    ctx = _FakeCtx(image_error=RuntimeError("provider 500"))
    await image_generate._process_once(ctx, store)

    job = _jobs(store)[0]
    assert job["status"] == "failed"
    assert "provider 500" in job["error"]
    assert not job.get("image_path")
    assert job["callback_state"] == "completed"
    assert ctx.callback_calls[0]["outcome"] == "error"
    assert ctx.callback_calls[0]["artifact_path"] == ""
    assert "provider 500" in ctx.callback_calls[0]["error"]
    assert ctx.dispatch_calls == []


@pytest.mark.asyncio
async def test_busy_agent_defers_callback_without_counting_a_failure_or_regenerating(tmp_path):
    store = _store(tmp_path)
    await image_generate.run(prompt="画一只猫", data_store=store, chat_context=_GROUP_CONTEXT)
    ctx = _FakeCtx(callback_results=[{"status": "deferred"}, {"status": "completed", "text": ""}])
    await image_generate._process_once(ctx, store)
    assert _jobs(store)[0]["callback_state"] == "pending"
    assert _jobs(store)[0]["callback_attempts"] == 0
    assert _jobs(store)[0]["notified"] is False
    await image_generate._process_once(ctx, ToolDataStore(store.store_path))

    assert _jobs(store)[0]["callback_state"] == "completed"
    assert len(ctx.image_calls) == 1
    assert len(ctx.callback_calls) == 2
    assert ctx.callback_calls[0]["callback_id"] == ctx.callback_calls[1]["callback_id"]
    assert ctx.dispatch_calls == []


@pytest.mark.asyncio
async def test_pure_model_failure_retries_three_times_without_regenerating_then_stops(tmp_path):
    store = _store(tmp_path)
    await image_generate.run(prompt="画一只猫", data_store=store, chat_context=_GROUP_CONTEXT)
    ctx = _FakeCtx(callback_error=RuntimeError("模型不可用"))
    for _ in range(5):
        await image_generate._process_once(ctx, ToolDataStore(store.store_path))

    assert _jobs(store)[0]["callback_state"] == "failed"
    assert _jobs(store)[0]["callback_attempts"] == 3
    assert _jobs(store)[0]["notified"] is False
    assert len(ctx.callback_calls) == 3
    assert len(ctx.image_calls) == 1
    assert ctx.dispatch_calls == []
    ctx.callback_error = None
    await image_generate.run(prompt="下一张猫", data_store=store, chat_context=_GROUP_CONTEXT)
    await image_generate._process_once(ctx, store)
    assert _jobs(store)[1]["callback_state"] == "completed", "失败的旧回调不能饿死后续任务"


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_before_tool_is_durable_and_post_tool_interruption_is_never_replayed(
    tmp_path, cancelled
):
    store = _store(tmp_path)
    await image_generate.run(prompt="画一只猫", data_store=store, chat_context=_GROUP_CONTEXT)
    ctx = _FakeCtx(
        unsafe=True,
        callback_error=asyncio.CancelledError() if cancelled else RuntimeError("工具后模型失败"),
    )

    def check_fence(_kwargs):
        persisted = _jobs(store)[0]
        assert persisted["callback_state"] == "running"
        assert persisted["callback_unsafe"] is True

    ctx.on_callback = check_fence
    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await image_generate._process_once(ctx, store)
    else:
        await image_generate._process_once(ctx, store)
    ctx.callback_error = None
    for _ in range(2):
        await image_generate._process_once(ctx, ToolDataStore(store.store_path))

    assert _jobs(store)[0]["callback_state"] == "uncertain"
    assert _jobs(store)[0]["notified"] is False
    assert len(ctx.callback_calls) == len(ctx.image_calls) == 1
    assert ctx.dispatch_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe", [False, True])
async def test_restart_recovers_callback_only_when_no_tool_could_have_run(tmp_path, unsafe):
    store = _store(tmp_path)
    image_path = tmp_path / "ready.png"
    image_path.write_bytes(_PNG_BYTES)
    store.set(
        "jobs",
        [
            {
                "id": "img_restart",
                "status": "done",
                "group_id": _GROUP_CONTEXT["group_id"],
                "prompt": "猫",
                "image_path": str(image_path),
                "notified": False,
                "callback_state": "running",
                "callback_unsafe": unsafe,
                "callback_attempts": 0,
            }
        ],
    )
    store.save()
    ctx = _FakeCtx()
    await image_generate._process_once(ctx, ToolDataStore(store.store_path))
    await image_generate._process_once(ctx, ToolDataStore(store.store_path))

    assert _jobs(store)[0]["callback_state"] == ("uncertain" if unsafe else "completed")
    assert len(ctx.callback_calls) == (0 if unsafe else 1)
    assert ctx.image_calls == ctx.dispatch_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("image_sent", [False, True])
@pytest.mark.parametrize("notify_turn_done", [False, True])
@pytest.mark.parametrize("notified", [False, True])
async def test_legacy_v2_results_never_resume_automatic_image_or_cached_text_delivery(
    tmp_path,
    image_sent,
    notify_turn_done,
    notified,
):
    store = _store(tmp_path)
    image_path = tmp_path / "legacy.png"
    image_path.write_bytes(_PNG_BYTES)
    store.set(
        "jobs",
        [
            {
                "id": "img_legacy",
                "status": "done",
                "delivery_version": 2,
                "group_id": _GROUP_CONTEXT["group_id"],
                "adapter_type": "napcat",
                "image_path": str(image_path),
                "image_sent": image_sent,
                "notify_turn_done": notify_turn_done,
                "result_text": "旧缓存正文绝不补发",
                "notified": notified,
            }
        ],
    )
    store.save()
    ctx = _FakeCtx()
    await image_generate._process_once(ctx, ToolDataStore(store.store_path))
    await image_generate._process_once(ctx, ToolDataStore(store.store_path))

    assert ctx.image_calls == ctx.dispatch_calls == []
    assert len(ctx.callback_calls) == (0 if notified else 1)
    if not notified:
        hint = ctx.callback_calls[0]["deliver_hint"]
        assert "已确认" in hint if image_sent else "不能确认" in hint
        assert "不要再次发送" in hint if image_sent else "不要盲目重发" in hint
        assert _jobs(store)[0]["callback_state"] == "completed"


@pytest.mark.asyncio
async def test_origin_self_initiated_permission_survives_persistence_and_callback(tmp_path):
    store = _store(tmp_path)
    await image_generate.run(
        prompt="我自己想画的猫",
        data_store=store,
        invocation_context=ToolInvocationContext(
            caller=UnifiedUser(user_id="u1", name="Alice"),
            group_id=_GROUP_CONTEXT["group_id"],
            adapter_type="napcat",
            self_initiated=True,
        ),
    )
    ctx = _FakeCtx()
    await image_generate._process_once(ctx, ToolDataStore(store.store_path))
    assert ctx.callback_calls[0]["self_initiated"] is True
    assert ctx.callback_calls[0]["caller_is_developer"] is False
    assert ctx.dispatch_calls == []


@pytest.mark.asyncio
async def test_reference_bytes_survive_queueing_and_are_passed_to_generator(tmp_path):
    store = _store(tmp_path)
    reference = tmp_path / "cat.png"
    reference.write_bytes(_PNG_BYTES)
    await image_generate.run(
        prompt="照这张图再画",
        reference_images=[str(reference)],
        data_store=store,
        chat_context=_GROUP_CONTEXT,
    )
    job = _jobs(store)[0]
    assert job["references"] == ["01_cat.png"]
    assert (store.artifact_dir / "references" / job["id"] / "01_cat.png").read_bytes() == _PNG_BYTES
    ctx = _FakeCtx()
    await image_generate._process_once(ctx, store)
    assert ctx.image_calls[0]["reference_images"] == [("01_cat.png", _PNG_BYTES)]


@pytest.mark.asyncio
async def test_job_submitted_during_generation_is_not_overwritten(tmp_path):
    store = _store(tmp_path)
    await image_generate.run(prompt="第一张猫", data_store=store, chat_context=_GROUP_CONTEXT)

    class SubmittingCtx(_FakeCtx):
        async def generate_image(self, **kwargs):
            await image_generate.run(prompt="第二张猫", data_store=store, chat_context=_GROUP_CONTEXT)
            return await super().generate_image(**kwargs)

    ctx = SubmittingCtx()
    await image_generate._process_once(ctx, store)
    jobs = _jobs(store)
    assert len(jobs) == 2
    assert jobs[0]["callback_state"] == "completed"
    assert jobs[1]["status"] == "pending"
    assert jobs[1]["prompt"] == "第二张猫"


@pytest.mark.asyncio
async def test_cancelled_generation_recovers_as_internal_error_without_regenerating(tmp_path):
    store = _store(tmp_path)
    await image_generate.run(prompt="画一束花", data_store=store, chat_context=_GROUP_CONTEXT)
    ctx = _FakeCtx(image_error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await image_generate._process_once(ctx, store)
    assert _jobs(store)[0]["status"] == "running"
    ctx.image_error = None
    await image_generate._process_once(ctx, ToolDataStore(store.store_path))
    assert _jobs(store)[0]["status"] == "failed"
    assert _jobs(store)[0]["error"] == "进程重启，任务中断"
    assert ctx.callback_calls[0]["outcome"] == "error"
    assert len(ctx.image_calls) == 1
    assert ctx.dispatch_calls == []


@pytest.mark.asyncio
async def test_job_without_origin_keeps_artifact_and_skips_callback(tmp_path):
    store = _store(tmp_path)
    await image_generate.run(prompt="画一颗星星", data_store=store)
    ctx = _FakeCtx()
    await image_generate._process_once(ctx, store)
    await image_generate._process_once(ctx, store)
    job = _jobs(store)[0]
    assert job["status"] == "done"
    assert Path(job["image_path"]).read_bytes() == _PNG_BYTES
    assert job["callback_state"] == "skipped"
    assert job["notified"] is False
    assert ctx.callback_calls == ctx.dispatch_calls == []
