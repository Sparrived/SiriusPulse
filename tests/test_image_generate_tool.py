"""后台生图 TOOL 的业务行为。

业务视角：
- 模型调用 ``image_generate`` 只拿到“已提交”，不必等几分钟的生图；
- 后台任务把图画好、落盘，再让模型在完成回合里用 ``group_file_exec`` 把图发回原群；
- 生成失败要如实通知；通知回合连续失败时至少把图片送达；
- 进程重启不会让中断的任务永远悬着，也不会丢掉通知。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from sirius_pulse.tools.builtin import image_generate
from sirius_pulse.tools.data_store import ToolDataStore

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-png-body"
_GROUP_CONTEXT = {"group_id": "987654321", "user_id": "u-1", "adapter_type": "napcat"}


def _store(tmp_path: Path) -> ToolDataStore:
    return ToolDataStore(tmp_path / "tool_data" / "image_generate.json")


def _jobs(store: ToolDataStore) -> list[dict[str, Any]]:
    return store.get("jobs", [])


class _FakeCtx:
    """记录引擎侧调用，按用例配置返回值或异常。"""

    def __init__(
        self,
        *,
        image_error: BaseException | None = None,
        notify_payload: dict[str, Any] | None = None,
        notify_errors: list[BaseException] | None = None,
        dispatch_results: list[bool] | None = None,
    ) -> None:
        self.image_error = image_error
        self.notify_payload = {"text": "画好啦"} if notify_payload is None else notify_payload
        self.notify_errors = list(notify_errors or [])
        self.dispatch_results = list(dispatch_results or [])
        self.image_calls: list[dict[str, Any]] = []
        self.notify_calls: list[dict[str, Any]] = []
        self.dispatch_calls: list[dict[str, Any]] = []

    async def generate_image(
        self,
        *,
        prompt: str,
        reference_images: list[tuple[str, bytes]] | None = None,
        timeout_seconds: float = 300.0,
    ) -> dict[str, Any]:
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

    async def generate_background_result_message(self, **kwargs: Any) -> dict[str, Any]:
        self.notify_calls.append(kwargs)
        if self.notify_errors:
            raise self.notify_errors.pop(0)
        return dict(self.notify_payload)

    async def dispatch_proactive_message(self, **kwargs: Any) -> bool:
        self.dispatch_calls.append(kwargs)
        return self.dispatch_results.pop(0) if self.dispatch_results else True


class _CtxWithStore:
    def __init__(self, store: ToolDataStore) -> None:
        self._store = store

    def get_data_store(self, tool_name: str) -> ToolDataStore:
        assert tool_name == "image_generate"
        return self._store


class _SubmittingCtx(_FakeCtx):
    """画第一张图的中途，模型又在另一个调用里提交了第二张。"""

    def __init__(self, store: ToolDataStore) -> None:
        super().__init__()
        self._store = store
        self._submitted = False

    async def generate_image(self, **kwargs: Any) -> dict[str, Any]:
        if not self._submitted:
            self._submitted = True
            await image_generate.run(
                prompt="再画一张猫",
                data_store=self._store,
                chat_context=_GROUP_CONTEXT,
            )
        return await super().generate_image(**kwargs)


def test_background_task_spec_polls_the_queue(tmp_path: Path):
    """后台任务是这套功能唯一的驱动源，注册失败就等于模型永远等不到图。"""
    store = _store(tmp_path)
    specs = image_generate.create_background_tasks(_CtxWithStore(store))

    assert len(specs) == 1
    assert specs[0].name == "image_generate_jobs"
    assert specs[0].interval_seconds == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_run_submits_pending_job_and_tells_model_how_to_deliver(tmp_path: Path):
    store = _store(tmp_path)

    result = await image_generate.run(
        prompt="画一只戴墨镜的橘猫",
        data_store=store,
        chat_context=_GROUP_CONTEXT,
    )

    assert result["success"] is True
    text = "\n".join(result["text_blocks"])
    assert "已提交" in text
    assert "group_file_exec" in text
    assert 'action="image"' in text

    persisted = ToolDataStore(store.store_path).get("jobs")
    assert len(persisted) == 1
    job = persisted[0]
    assert job["status"] == "pending"
    assert job["prompt"] == "画一只戴墨镜的橘猫"
    assert job["id"].startswith("img_")
    assert job["group_id"] == _GROUP_CONTEXT["group_id"]
    assert job["notified"] is False


@pytest.mark.asyncio
async def test_worker_saves_image_and_notifies_group_through_model_turn(tmp_path: Path):
    store = _store(tmp_path)
    await image_generate.run(prompt="画一张海边日落", data_store=store, chat_context=_GROUP_CONTEXT)
    ctx = _FakeCtx(notify_payload={"text": "日落画好了，看看", "image_sent": True})

    await image_generate._process_once(ctx, store)

    job = _jobs(store)[0]
    assert job["status"] == "done"
    image_path = Path(job["image_path"])
    assert image_path.is_file()
    assert image_path.read_bytes() == _PNG_BYTES
    assert image_path == store.artifact_dir / "images" / f"{job['id']}.png"

    assert ctx.image_calls[0]["prompt"] == "画一张海边日落"
    assert ctx.image_calls[0]["reference_images"] == []
    assert len(ctx.notify_calls) == 1
    notify = ctx.notify_calls[0]
    assert notify["outcome"] == "success"
    assert "group_file_exec" in notify["deliver_hint"]
    assert job["image_path"] in notify["deliver_hint"]
    assert notify["artifact_path"] == job["image_path"]
    assert notify["group_id"] == _GROUP_CONTEXT["group_id"]
    assert notify["request_summary"] == "画一张海边日落"

    assert len(ctx.dispatch_calls) == 1
    dispatched = ctx.dispatch_calls[0]
    assert dispatched["group_id"] == _GROUP_CONTEXT["group_id"]
    assert dispatched["text"] == "日落画好了，看看"
    assert dispatched["event_id"] == f"image_generate:{job['id']}"
    assert dispatched["image_path"] == "", "模型已经用 group_file_exec 发过图，不能再重复带图"
    assert job["notified"] is True


@pytest.mark.asyncio
async def test_generation_failure_is_reported_honestly(tmp_path: Path):
    store = _store(tmp_path)
    await image_generate.run(prompt="画一张不存在的图", data_store=store, chat_context=_GROUP_CONTEXT)
    ctx = _FakeCtx(
        image_error=RuntimeError("provider 500"),
        notify_payload={"text": "抱歉，这次没画成"},
    )

    await image_generate._process_once(ctx, store)

    job = _jobs(store)[0]
    assert job["status"] == "failed"
    assert "provider 500" in job["error"]
    assert job.get("image_path") in (None, "")

    assert len(ctx.notify_calls) == 1
    notify = ctx.notify_calls[0]
    assert notify["outcome"] == "error"
    assert "没画成" in notify["deliver_hint"]
    assert "provider 500" in notify["error"]
    assert notify["artifact_path"] == ""

    assert ctx.dispatch_calls[0]["text"] == "抱歉，这次没画成"
    assert job["notified"] is True


@pytest.mark.asyncio
async def test_notification_turn_failures_fall_back_to_image_only_delivery(tmp_path: Path):
    store = _store(tmp_path)
    await image_generate.run(prompt="画一只柴犬", data_store=store, chat_context=_GROUP_CONTEXT)
    ctx = _FakeCtx(notify_errors=[RuntimeError("通知回合不可用")] * 3)

    await image_generate._process_once(ctx, store)
    await image_generate._process_once(ctx, store)
    await image_generate._process_once(ctx, store)
    job = _jobs(store)[0]
    assert job["notify_attempts"] == 3
    assert job["notified"] is False
    assert ctx.dispatch_calls == []

    await image_generate._process_once(ctx, store)

    job = _jobs(store)[0]
    assert len(ctx.notify_calls) == 3
    assert job["notified"] is True
    assert job["fallback"] is True
    assert len(ctx.dispatch_calls) == 1
    fallback = ctx.dispatch_calls[0]
    assert fallback["text"] == ""
    assert fallback["image_path"] == job["image_path"]
    assert fallback["event_id"] == f"image_generate:{job['id']}"


@pytest.mark.asyncio
async def test_restart_marks_interrupted_job_failed_and_still_notifies(tmp_path: Path):
    store = _store(tmp_path)
    store.set(
        "jobs",
        [
            {
                "id": "img_deadbeef",
                "status": "running",
                "prompt": "画一只睡着的狗",
                "references": [],
                "group_id": _GROUP_CONTEXT["group_id"],
                "user_id": "",
                "user_name": "",
                "adapter_type": "napcat",
                "caller_is_developer": False,
                "created_at": "2026-01-01T00:00:00+00:00",
                "notified": False,
                "notify_attempts": 0,
            }
        ],
    )
    store.save()
    ctx = _FakeCtx(notify_payload={"text": "上次那张图中断了，这次没画成"})

    await image_generate._process_once(ctx, store)

    job = _jobs(store)[0]
    assert job["status"] == "failed"
    assert job["error"] == "进程重启，任务中断"
    assert ctx.image_calls == [], "中断的任务不能悄悄重跑一次生图"
    assert ctx.notify_calls[0]["outcome"] == "error"
    assert ctx.notify_calls[0]["request_summary"] == "画一只睡着的狗"
    assert ctx.dispatch_calls[0]["text"] == "上次那张图中断了，这次没画成"
    assert job["notified"] is True


@pytest.mark.asyncio
async def test_without_chat_context_image_is_still_saved_but_no_message_is_sent(tmp_path: Path):
    store = _store(tmp_path)
    await image_generate.run(prompt="画一颗星星", data_store=store)
    ctx = _FakeCtx()

    await image_generate._process_once(ctx, store)

    job = _jobs(store)[0]
    assert job["status"] == "done"
    assert Path(job["image_path"]).is_file()
    assert job["notified"] is True
    assert ctx.notify_calls == []
    assert ctx.dispatch_calls == []


@pytest.mark.asyncio
async def test_reference_image_is_materialized_and_passed_to_generator(tmp_path: Path):
    store = _store(tmp_path)
    reference = tmp_path / "cat.png"
    reference.write_bytes(_PNG_BYTES)

    await image_generate.run(
        prompt="照着这张图再画一张",
        reference_images=[str(reference)],
        data_store=store,
        chat_context=_GROUP_CONTEXT,
    )

    job = _jobs(store)[0]
    assert job["references"] == ["01_cat.png"]
    stored_reference = store.artifact_dir / "references" / job["id"] / job["references"][0]
    assert stored_reference.read_bytes() == _PNG_BYTES

    ctx = _FakeCtx()
    await image_generate._process_once(ctx, store)

    assert ctx.image_calls[0]["reference_images"] == [("01_cat.png", _PNG_BYTES)]


@pytest.mark.asyncio
async def test_worker_passes_cancellation_through_and_keeps_job_for_recovery(tmp_path: Path):
    """引擎关停时取消后台任务，不能把 CancelledError 吞成普通失败。"""
    store = _store(tmp_path)
    await image_generate.run(prompt="画一束花", data_store=store, chat_context=_GROUP_CONTEXT)
    ctx = _FakeCtx(image_error=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await image_generate._process_once(ctx, store)

    assert _jobs(store)[0]["status"] == "running", "留给下次进程启动做中断恢复"


@pytest.mark.asyncio
async def test_job_submitted_while_worker_is_generating_is_not_lost(tmp_path: Path):
    """生图要跑很久，期间模型可以再提交一张；后台写回不能覆盖掉新任务。"""
    store = _store(tmp_path)
    await image_generate.run(prompt="画第一张图", data_store=store, chat_context=_GROUP_CONTEXT)
    ctx = _SubmittingCtx(store)

    await image_generate._process_once(ctx, store)

    jobs = _jobs(store)
    assert len(jobs) == 2
    first = next(job for job in jobs if job["prompt"] == "画第一张图")
    second = next(job for job in jobs if job["prompt"] == "再画一张猫")
    assert first["status"] == "done"
    assert first["notified"] is True
    assert second["status"] == "pending"


@pytest.mark.asyncio
async def test_model_forgetting_to_send_the_image_is_backfilled_with_the_text(tmp_path: Path):
    """通知回合只说了话却没发图：图和文案要由框架一起补发，不能让图只留在磁盘上。"""
    store = _store(tmp_path)
    await image_generate.run(prompt="画一张海边日落", data_store=store, chat_context=_GROUP_CONTEXT)
    ctx = _FakeCtx(notify_payload={"text": "日落画好了，看看", "image_sent": False})

    await image_generate._process_once(ctx, store)

    job = _jobs(store)[0]
    assert job["notified"] is True
    assert job["fallback"] is True
    assert len(ctx.dispatch_calls) == 1
    dispatched = ctx.dispatch_calls[0]
    assert dispatched["text"] == "日落画好了，看看"
    assert dispatched["image_path"] == job["image_path"]


@pytest.mark.asyncio
async def test_fallback_delivery_gives_up_after_repeated_failures(tmp_path: Path):
    """适配器长期不可达时，队首任务不能无限重试把后面已完成任务的通知全饿死。"""
    store = _store(tmp_path)
    await image_generate.run(prompt="画一张海边日落", data_store=store, chat_context=_GROUP_CONTEXT)
    ctx = _FakeCtx(
        notify_errors=[RuntimeError("通知回合不可用")] * image_generate._NOTIFY_MAX_ATTEMPTS,
        dispatch_results=[False] * (image_generate._FALLBACK_MAX_ATTEMPTS + 1),
    )

    for _ in range(image_generate._FALLBACK_MAX_ATTEMPTS):
        await image_generate._process_once(ctx, store)

    job = _jobs(store)[0]
    assert job["notified"] is True
    assert job["fallback_failed"] is True
    assert job["notify_attempts"] == image_generate._FALLBACK_MAX_ATTEMPTS
    # 降级只尝试到上限为止，而不是每个 tick 都刷一条 warning。
    assert len(ctx.dispatch_calls) == image_generate._FALLBACK_MAX_ATTEMPTS - (
        image_generate._NOTIFY_MAX_ATTEMPTS
    )
