"""业务视角：交给后台的任务——超时转后台的 bash 命令与后台生图——能被看见、能被停下。

她看到的是工具结果和后台回调；外部可观察的是进程有没有被结束、生图有没有被调用、
回调有没有发生。
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import pytest

from sirius_pulse.memory.user.unified_models import UnifiedUser
from sirius_pulse.tools.builtin import background_task, bash, image_generate
from sirius_pulse.tools.builtin._internal import _bash_background, _bash_runtime
from sirius_pulse.tools.data_store import ToolDataStore
from sirius_pulse.tools.models import ToolInvocationContext


class _BashStore:
    def __init__(self, root: Path, **data: Any) -> None:
        self.store_path = root / "tool_data" / "bash.json"
        self.data = data

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


class _SlowProcess:
    """一直跑到测试让它结束为止的命令；没有真实 pid，停止只能走 terminate/kill。"""

    pid = None

    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path
        self.returncode: int | None = None
        self.terminated = False

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired("bash", timeout or 0)
        return self.returncode

    def poll(self) -> int | None:
        return self.returncode

    def finish(self, returncode: int, output: bytes = b"") -> None:
        with open(self.log_path, "ab") as log:
            log.write(output)
        self.returncode = returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    kill = terminate


class _Ctx:
    def __init__(self, tmp_path: Path, **bash_config: Any) -> None:
        self.stores = {
            "bash": _BashStore(tmp_path, **bash_config),
            "image_generate": ToolDataStore(tmp_path / "tool_data" / "image_generate.json"),
            "wake_me": ToolDataStore(tmp_path / "tool_data" / "wake_me.json"),
        }
        self.callbacks: list[dict[str, Any]] = []
        self.generation_started = asyncio.Event()
        self.generations = 0

    def get_data_store(self, name: str) -> Any:
        return self.stores[name]

    async def handle_background_tool_result(self, **kwargs: Any) -> dict[str, Any]:
        self.callbacks.append(kwargs)
        return {"status": "completed"}

    async def generate_image(self, **_: Any) -> dict[str, Any]:
        self.generations += 1
        self.generation_started.set()
        await asyncio.sleep(3600)
        return {"image_bytes": b"never", "mime_type": "image/png"}


def _caller(group_id: str = "g1", *, developer: bool = False) -> ToolInvocationContext:
    return ToolInvocationContext(
        caller=UnifiedUser(user_id="u1", name="小明", metadata={"is_developer": developer}),
        group_id=group_id,
        adapter_type="napcat",
    )


def _slow_bash(monkeypatch) -> list[_SlowProcess]:
    started: list[_SlowProcess] = []

    def fake_popen(args, **kwargs):
        kwargs["stdout"].write(b"starting\n")
        process = _SlowProcess(Path(kwargs["stdout"].name))
        started.append(process)
        return process

    monkeypatch.setattr(_bash_runtime, "find_bash", lambda: "bash")
    monkeypatch.setattr(bash.subprocess, "Popen", fake_popen)
    return started


async def _run_bash(ctx: _Ctx, command: str = "make build", **kwargs: Any) -> dict[str, Any]:
    return await bash.run(
        command,
        timeout_seconds=0.1,
        data_store=ctx.stores["bash"],
        engine_context=ctx,
        invocation_context=kwargs.pop("invocation_context", _caller()),
        **kwargs,
    )


async def _task(ctx: _Ctx, action: str, task_id: str = "", **kwargs: Any) -> dict[str, Any]:
    return await background_task.run(
        action,
        task_id,
        engine_context=ctx,
        invocation_context=kwargs.pop("invocation_context", _caller()),
    )


def _owner(ctx: _Ctx) -> str:
    return _bash_background.owner_key(ctx.stores["bash"])


@pytest.mark.asyncio
async def test_slow_command_moves_to_background_and_reports_back_once_when_done(
    monkeypatch, tmp_path: Path
) -> None:
    processes = _slow_bash(monkeypatch)
    ctx = _Ctx(tmp_path)

    result = await _run_bash(ctx)

    task_id = result["data"]["task_id"]
    assert result["success"] is True and task_id.startswith("bash_")
    assert task_id in result["text_blocks"][0] and "starting" in result["text_blocks"][0]
    assert not processes[0].terminated

    processes[0].finish(0, b"build ok\n")
    await _bash_background.process_once(ctx, _owner(ctx))
    await _bash_background.process_once(ctx, _owner(ctx))

    assert len(ctx.callbacks) == 1
    callback = ctx.callbacks[0]
    assert callback["callback_id"] == f"bash:{task_id}"
    assert callback["outcome"] == "success"
    assert "build ok" in callback["output"]
    assert callback["group_id"] == "g1" and callback["user_id"] == "u1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "config", "expected"),
    [
        ({"background_on_timeout": False}, {}, "命令执行超时"),
        ({}, {"max_background_jobs": 0}, "命令执行超时"),
    ],
)
async def test_timeout_without_background_ends_the_command(
    monkeypatch, tmp_path: Path, kwargs: dict, config: dict, expected: str
) -> None:
    processes = _slow_bash(monkeypatch)
    ctx = _Ctx(tmp_path, **config)

    result = await _run_bash(ctx, **kwargs)

    assert result["success"] is False and expected in result["error"]
    assert "starting" in result["error"]
    assert processes[0].terminated
    assert _bash_background.list_jobs(_owner(ctx)) == []


@pytest.mark.asyncio
async def test_background_slots_are_limited(monkeypatch, tmp_path: Path) -> None:
    processes = _slow_bash(monkeypatch)
    ctx = _Ctx(tmp_path, max_background_jobs=1)

    first = await _run_bash(ctx)
    second = await _run_bash(ctx, "make test")

    assert first["success"] is True
    assert second["success"] is False and "后台任务已满" in second["error"]
    assert processes[1].terminated and not processes[0].terminated


@pytest.mark.asyncio
async def test_she_can_list_and_stop_a_background_command(monkeypatch, tmp_path: Path) -> None:
    processes = _slow_bash(monkeypatch)
    ctx = _Ctx(tmp_path)
    task_id = (await _run_bash(ctx, "tail -f server.log"))["data"]["task_id"]

    listed = await _task(ctx, "list")
    status = await _task(ctx, "status", task_id)
    stopped = await _task(ctx, "stop", task_id)
    await _bash_background.process_once(ctx, _owner(ctx))

    assert task_id in listed["text_blocks"][0] and "tail -f server.log" in listed["text_blocks"][0]
    assert "starting" in status["text_blocks"][0]
    assert stopped["data"]["stopped"] is True
    assert processes[0].terminated
    assert ctx.callbacks == []  # 停的人已经知道结果，不再回调
    assert (await _task(ctx, "list"))["data"]["tasks"] == []


@pytest.mark.asyncio
async def test_another_chat_cannot_see_or_stop_her_background_work(
    monkeypatch, tmp_path: Path
) -> None:
    processes = _slow_bash(monkeypatch)
    ctx = _Ctx(tmp_path)
    task_id = (await _run_bash(ctx))["data"]["task_id"]
    other_chat = _caller("g2")

    listed = await _task(ctx, "list", invocation_context=other_chat)
    stopped = await _task(ctx, "stop", task_id, invocation_context=other_chat)
    by_developer = await _task(
        ctx, "stop", task_id, invocation_context=_caller("g2", developer=True)
    )

    assert listed["data"]["tasks"] == []
    assert stopped["success"] is False and "找不到任务" in stopped["error"]
    assert by_developer["data"]["stopped"] is True and processes[0].terminated


@pytest.mark.asyncio
async def test_background_command_is_ended_when_it_outlives_its_limit(
    monkeypatch, tmp_path: Path
) -> None:
    processes = _slow_bash(monkeypatch)
    ctx = _Ctx(tmp_path)
    task_id = (await _run_bash(ctx))["data"]["task_id"]
    _bash_background.get(_owner(ctx), task_id).deadline = 0.0

    await _bash_background.process_once(ctx, _owner(ctx))

    assert processes[0].terminated
    assert ctx.callbacks[0]["outcome"] == "error"
    assert "存活上限" in ctx.callbacks[0]["error"]


@pytest.mark.asyncio
async def test_persona_shutdown_ends_her_background_commands(monkeypatch, tmp_path: Path) -> None:
    processes = _slow_bash(monkeypatch)
    ctx = _Ctx(tmp_path)
    await _run_bash(ctx)

    bash.create_on_unload(ctx)

    assert processes[0].terminated


def _queue_image_job(ctx: _Ctx, job_id: str, *, group_id: str = "g1") -> None:
    store = ctx.stores["image_generate"]
    store.set(
        "jobs",
        [
            {
                "id": job_id,
                "status": "pending",
                "prompt": "月光下的猫",
                "references": [],
                "group_id": group_id,
                "user_id": "u1",
                "created_at": "2026-11-14T00:00:00+00:00",
                "delivery_version": 3,
                "callback_state": "pending",
                "callback_attempts": 0,
                "notified": False,
            }
        ],
    )
    store.save()


def _image_job(ctx: _Ctx, job_id: str) -> dict[str, Any]:
    return next(
        job for job in image_generate.list_jobs(ctx.stores["image_generate"]) if job["id"] == job_id
    )


@pytest.mark.asyncio
async def test_queued_image_is_withdrawn_before_anything_is_generated(tmp_path: Path) -> None:
    ctx = _Ctx(tmp_path)
    _queue_image_job(ctx, "img_cat00001")

    listed = await _task(ctx, "list")
    stopped = await _task(ctx, "stop", "img_cat00001")
    await image_generate._process_once(ctx, ctx.stores["image_generate"])

    assert "img_cat00001" in listed["text_blocks"][0] and "月光下的猫" in listed["text_blocks"][0]
    assert stopped["data"]["stopped"] is True
    assert ctx.generations == 0 and ctx.callbacks == []
    assert _image_job(ctx, "img_cat00001")["status"] == "cancelled"


@pytest.mark.asyncio
async def test_image_being_drawn_can_be_interrupted(tmp_path: Path) -> None:
    ctx = _Ctx(tmp_path)
    _queue_image_job(ctx, "img_cat00002")
    worker = asyncio.create_task(image_generate._process_once(ctx, ctx.stores["image_generate"]))
    await asyncio.wait_for(ctx.generation_started.wait(), timeout=1)

    stopped = await _task(ctx, "stop", "img_cat00002")
    await asyncio.wait_for(worker, timeout=1)
    await image_generate._process_once(ctx, ctx.stores["image_generate"])

    job = _image_job(ctx, "img_cat00002")
    assert "打断" in stopped["text_blocks"][0]
    assert job["status"] == "cancelled" and job["error_code"] == image_generate.STOP_CODE
    assert ctx.callbacks == []


@pytest.mark.asyncio
async def test_finished_image_cannot_be_stopped_again(tmp_path: Path) -> None:
    ctx = _Ctx(tmp_path)
    _queue_image_job(ctx, "img_cat00003")
    image_generate._update_job(ctx.stores["image_generate"], "img_cat00003", status="done")

    stopped = await _task(ctx, "stop", "img_cat00003")

    assert stopped["data"]["stopped"] is False
    assert "已经结束" in stopped["text_blocks"][0]


@pytest.mark.skipif(_bash_runtime.find_bash() is None, reason="需要真实 Bash")
def test_stopping_ends_the_whole_process_tree(tmp_path: Path) -> None:
    """管道里的子进程也要一起结束，不能只杀掉最外层的 bash。"""
    process = _bash_background.spawn(
        [_bash_runtime.find_bash(), "-c", "sleep 30 | cat"],
        cwd=str(tmp_path),
        env=None,
        log_path=tmp_path / "tree.log",
    )

    _bash_background.terminate_tree(process, grace_seconds=1.0)

    assert process.poll() is not None
