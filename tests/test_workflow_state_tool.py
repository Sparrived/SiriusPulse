"""Tests for the reliable, chat-scoped workflow state machine."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sirius_pulse.memory.user.unified_models import UnifiedUser
from sirius_pulse.tools import ToolExecutor, ToolInvocationContext, ToolRegistry
from sirius_pulse.tools.models import ToolResult


def _context(user_id: str = "u1") -> ToolInvocationContext:
    return ToolInvocationContext(caller=UnifiedUser(user_id=user_id, name=user_id))


def _tool(tmp_path: Path):
    registry = ToolRegistry()
    registry.load_from_directory(
        tmp_path / "tools",
        auto_install_deps=False,
        include_builtin=True,
    )
    tool = registry.get("workflow_state")
    assert tool is not None
    return tool


def _executor(tmp_path: Path, group_id: str = "group-1", user_id: str = "u1") -> ToolExecutor:
    executor = ToolExecutor(tmp_path)
    executor.set_chat_context(group_id=group_id, user_id=user_id, adapter_type="napcat")
    return executor


def _run(executor: ToolExecutor, tool, **params):
    return executor.execute(tool, params, invocation_context=_context(params.pop("user_id", "u1")))


def test_workflow_state_begin_resume_and_begin_reuse(tmp_path: Path):
    tool = _tool(tmp_path)
    executor = _executor(tmp_path)

    created = _run(
        executor,
        tool,
        action="begin",
        key="serial_novel.chapter",
        version="1",
        state_json=json.dumps({"chapter": 7, "target": "draft.md"}),
    )

    assert created.success is True
    assert created.data["created"] is True
    workflow = created.data["workflow"]
    assert workflow["revision"] == 0
    assert workflow["status"] == "active"
    assert workflow["inputs"] == {"chapter": 7, "target": "draft.md"}

    next_executor = _executor(tmp_path, user_id="u2")
    resumed = _run(next_executor, tool, action="resume", key="serial_novel.chapter", version="1")
    assert resumed.success is True
    assert resumed.data["resumable"] is True
    assert resumed.data["workflow"]["workflow_id"] == workflow["workflow_id"]
    assert resumed.data["workflow"]["revision"] == 0

    reused = _run(
        next_executor,
        tool,
        action="begin",
        key="serial_novel.chapter",
        version="1",
        state_json=json.dumps({"chapter": 999}),
    )
    assert reused.success is True
    assert reused.data["created"] is False
    assert reused.data["reused"] is True
    assert reused.data["workflow"]["inputs"]["chapter"] == 7


def test_workflow_state_lists_registered_workflows_for_current_chat(tmp_path: Path):
    tool = _tool(tmp_path)
    executor = _executor(tmp_path)
    created = _run(
        executor,
        tool,
        action="begin",
        key="napcat.like:123456",
        summary="给指定 QQ 点赞",
        state_json=json.dumps({"user_id": 123456, "token": "must-not-list"}),
    )

    assert created.success is True
    assert created.data["registered"] is True

    listed = _run(executor, tool, action="list")

    assert listed.success is True
    assert listed.data["registered"] is True
    assert listed.data["count"] == 1
    item = listed.data["workflows"][0]
    assert item["key"] == "napcat.like:123456"
    assert item["status"] == "active"
    assert item["summary"] == "给指定 QQ 点赞"
    assert "token" not in json.dumps(listed.data, ensure_ascii=False)

    filtered = _run(executor, tool, action="list", key="napcat.like:123456")
    missing = _run(executor, tool, action="list", key="serial_novel:missing")
    assert filtered.data["count"] == 1
    assert missing.data["registered"] is False

    other_chat = _executor(tmp_path, group_id="group-2")
    assert _run(other_chat, tool, action="list").data["count"] == 0


def test_workflow_state_claim_prevents_duplicate_side_effects(tmp_path: Path):
    tool = _tool(tmp_path)
    executor = _executor(tmp_path)
    assert _run(executor, tool, action="begin", key="napcat.like").success is True

    claim = _run(
        executor,
        tool,
        action="claim",
        key="napcat.like",
        step="like",
        tool_name="qq_like",
        idempotency_key="napcat.like:123:1",
        expected_revision=0,
        state_json=json.dumps({"user_id": 123, "times": 1}),
    )
    assert claim.success is True
    assert claim.data["claimed"] is True
    assert claim.data["claim_token"]
    assert claim.data["revision"] == 1
    assert claim.data["next_step"] == "like"
    visible = ToolResult.from_raw_result(claim.data).to_model_text(max_chars=500)
    assert visible.index("revision:") < visible.index("workflow:")
    assert claim.data["workflow"]["revision"] == 1

    duplicate = _run(
        executor,
        tool,
        action="claim",
        key="napcat.like",
        step="like",
        tool_name="qq_like",
        idempotency_key="napcat.like:123:1",
        expected_revision=1,
    )
    assert duplicate.success is True
    assert duplicate.data["claimed"] is False
    assert duplicate.data["in_progress"] is True

    different_target = _run(
        executor,
        tool,
        action="claim",
        key="napcat.like",
        step="like",
        tool_name="qq_like",
        idempotency_key="napcat.like:456:1",
        expected_revision=1,
    )
    assert different_target.success is False
    assert different_target.data["conflict"] is True


def test_workflow_state_concurrent_claims_allow_only_one_revision_winner(tmp_path: Path):
    tool = _tool(tmp_path)
    executor = _executor(tmp_path)
    assert _run(executor, tool, action="begin", key="napcat.like").success is True
    executor.get_data_store("workflow_state")

    def claim_once():
        return _run(
            executor,
            tool,
            action="claim",
            key="napcat.like",
            step="like",
            tool_name="qq_like",
            idempotency_key="like:123:1",
            expected_revision=0,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim_once(), range(2)))

    assert sum(result.success for result in results) == 1
    assert (
        sum(result.success is False and result.data.get("conflict") is True for result in results)
        == 1
    )


def test_workflow_state_stale_claim_token_cannot_write_after_reclaim(tmp_path: Path, monkeypatch):
    tool = _tool(tmp_path)
    executor = _executor(tmp_path)
    _run(executor, tool, action="begin", key="napcat.like")

    clock = {"now": 1000.0}
    monkeypatch.setitem(tool._run_func.__globals__, "_now", lambda: clock["now"])
    first = _run(
        executor,
        tool,
        action="claim",
        key="napcat.like",
        step="like",
        tool_name="qq_like",
        idempotency_key="like:123:1",
        lease_seconds=30,
        expected_revision=0,
    )
    clock["now"] = 1031.0
    reclaimed = _run(
        executor,
        tool,
        action="claim",
        key="napcat.like",
        step="like",
        tool_name="qq_like",
        idempotency_key="like:123:1",
        lease_seconds=30,
        expected_revision=1,
    )
    assert reclaimed.success is True
    assert reclaimed.data["claim_token"] != first.data["claim_token"]
    assert first.data["workflow"]["revision"] == 1
    assert first.data["workflow"]["steps"]["like"]["claim_token"] == first.data["claim_token"]

    stale = _run(
        executor,
        tool,
        action="checkpoint",
        key="napcat.like",
        step="like",
        idempotency_key="like:123:1",
        claim_token=first.data["claim_token"],
        expected_revision=2,
        state_json='{"message_id":"stale"}',
    )
    assert stale.success is False
    assert stale.data["conflict"] is True


def test_workflow_state_checkpoint_is_idempotent_and_empty_next_step_completes(
    tmp_path: Path,
):
    tool = _tool(tmp_path)
    executor = _executor(tmp_path)
    _run(executor, tool, action="begin", key="napcat.like")
    claim = _run(
        executor,
        tool,
        action="claim",
        key="napcat.like",
        step="like",
        tool_name="qq_like",
        idempotency_key="like:123:1",
        expected_revision=0,
    )

    checkpoint = _run(
        executor,
        tool,
        action="checkpoint",
        key="napcat.like",
        step="like",
        idempotency_key="like:123:1",
        expected_revision=1,
        claim_token=claim.data["claim_token"],
        next_step="publish",
        state_json=json.dumps({"message_id": "m1", "token": "must-not-persist"}),
        summary="点赞成功",
    )
    assert checkpoint.success is True
    assert checkpoint.data["checkpointed"] is True
    assert checkpoint.data["replayed"] is False
    assert checkpoint.data["completed"] is False
    assert checkpoint.data["workflow"]["steps"]["like"]["result"] == {"message_id": "m1"}
    assert checkpoint.data["workflow"]["revision"] == 2

    replay = _run(
        executor,
        tool,
        action="checkpoint",
        key="napcat.like",
        step="like",
        idempotency_key="like:123:1",
        expected_revision=2,
        claim_token=claim.data["claim_token"],
        next_step="publish",
        state_json=json.dumps({"message_id": "different"}),
    )
    assert replay.success is True
    assert replay.data["replayed"] is True
    assert replay.data["result"] == {"message_id": "m1"}

    publish_claim = _run(
        executor,
        tool,
        action="claim",
        key="napcat.like",
        step="publish",
        tool_name="qq_like",
        idempotency_key="like:123:publish",
        expected_revision=2,
    )
    completed = _run(
        executor,
        tool,
        action="checkpoint",
        key="napcat.like",
        step="publish",
        tool_name="qq_like",
        idempotency_key="like:123:publish",
        expected_revision=3,
        claim_token=publish_claim.data["claim_token"],
    )
    assert completed.success is True
    assert completed.data["completed"] is True
    assert completed.data["workflow"]["status"] == "completed"
    completed_again = _run(
        executor,
        tool,
        action="checkpoint",
        key="napcat.like",
        step="publish",
        tool_name="qq_like",
        idempotency_key="like:123:publish",
        expected_revision=4,
        claim_token=publish_claim.data["claim_token"],
    )
    assert completed_again.success is True
    assert completed_again.data["replayed"] is True
    assert completed_again.data["workflow"]["revision"] == 4
    resumed = _run(executor, tool, action="resume", key="napcat.like")
    assert resumed.success is True
    assert resumed.data["resumable"] is False


def test_workflow_state_checkpoint_with_next_step_keeps_workflow_active(tmp_path: Path):
    tool = _tool(tmp_path)
    executor = _executor(tmp_path)
    _run(executor, tool, action="begin", key="serial_novel.chapter")
    claim = _run(
        executor,
        tool,
        action="claim",
        key="serial_novel.chapter",
        step="write",
        tool_name="bash",
        idempotency_key="chapter:7:write",
        expected_revision=0,
    )
    checkpoint = _run(
        executor,
        tool,
        action="checkpoint",
        key="serial_novel.chapter",
        step="write",
        idempotency_key="chapter:7:write",
        next_step="publish",
        expected_revision=1,
        claim_token=claim.data["claim_token"],
    )
    assert checkpoint.success is True
    assert checkpoint.data["completed"] is False
    assert checkpoint.data["next_step"] == "publish"
    assert checkpoint.data["workflow"]["status"] == "active"


def test_workflow_state_rejects_claiming_a_step_other_than_next_step(tmp_path: Path):
    tool = _tool(tmp_path)
    executor = _executor(tmp_path)
    _run(executor, tool, action="begin", key="serial_novel.chapter")

    first = _run(
        executor,
        tool,
        action="claim",
        key="serial_novel.chapter",
        step="write",
        tool_name="bash",
        idempotency_key="chapter:7:write",
        expected_revision=0,
    )
    checkpoint = _run(
        executor,
        tool,
        action="checkpoint",
        key="serial_novel.chapter",
        step="write",
        tool_name="bash",
        idempotency_key="chapter:7:write",
        claim_token=first.data["claim_token"],
        expected_revision=1,
        next_step="publish",
    )
    skipped = _run(
        executor,
        tool,
        action="claim",
        key="serial_novel.chapter",
        step="write",
        tool_name="bash",
        idempotency_key="chapter:7:write:again",
        expected_revision=2,
    )

    assert checkpoint.success is True
    assert skipped.success is False
    assert skipped.data["conflict"] is True
    assert "publish" in skipped.error


def test_workflow_state_rejects_side_effects_after_completion_without_restart(tmp_path: Path):
    tool = _tool(tmp_path)
    executor = _executor(tmp_path)
    _run(executor, tool, action="begin", key="napcat.like")
    claim = _run(
        executor,
        tool,
        action="claim",
        key="napcat.like",
        step="like",
        tool_name="qq_like",
        idempotency_key="like:123:1",
        expected_revision=0,
    )
    _run(
        executor,
        tool,
        action="checkpoint",
        key="napcat.like",
        step="like",
        tool_name="qq_like",
        idempotency_key="like:123:1",
        claim_token=claim.data["claim_token"],
        expected_revision=1,
    )
    blocked = _run(
        executor,
        tool,
        action="claim",
        key="napcat.like",
        step="like",
        tool_name="qq_like",
        idempotency_key="like:123:1:again",
        expected_revision=2,
    )

    assert blocked.success is False
    assert "restart" in blocked.error


def test_workflow_state_requires_actionable_tool_and_valid_next_step(tmp_path: Path):
    tool = _tool(tmp_path)
    executor = _executor(tmp_path)
    _run(executor, tool, action="begin", key="serial_novel.chapter")

    missing_tool = _run(
        executor,
        tool,
        action="claim",
        key="serial_novel.chapter",
        step="write",
        idempotency_key="chapter:7:write",
        expected_revision=0,
    )
    assert missing_tool.success is False
    assert "tool_name" in missing_tool.error

    claim = _run(
        executor,
        tool,
        action="claim",
        key="serial_novel.chapter",
        step="write",
        tool_name="bash",
        idempotency_key="chapter:7:write",
        expected_revision=0,
    )
    invalid_next_step = _run(
        executor,
        tool,
        action="checkpoint",
        key="serial_novel.chapter",
        step="write",
        tool_name="bash",
        idempotency_key="chapter:7:write",
        claim_token=claim.data["claim_token"],
        expected_revision=1,
        next_step="../publish",
    )

    assert invalid_next_step.success is False
    assert "next_step" in invalid_next_step.error


def test_workflow_state_failure_preserves_last_success_and_can_reclaim(tmp_path: Path):
    tool = _tool(tmp_path)
    executor = _executor(tmp_path)
    _run(executor, tool, action="begin", key="serial_novel.chapter")
    write_claim = _run(
        executor,
        tool,
        action="claim",
        key="serial_novel.chapter",
        step="write",
        tool_name="bash",
        idempotency_key="chapter:7:write",
        expected_revision=0,
    )
    _run(
        executor,
        tool,
        action="checkpoint",
        key="serial_novel.chapter",
        step="write",
        idempotency_key="chapter:7:write",
        expected_revision=1,
        claim_token=write_claim.data["claim_token"],
        next_step="publish",
        state_json=json.dumps({"path": "draft.md"}),
    )
    publish_claim = _run(
        executor,
        tool,
        action="claim",
        key="serial_novel.chapter",
        step="publish",
        tool_name="group_file_exec",
        idempotency_key="chapter:7:publish",
        expected_revision=2,
    )
    failed = _run(
        executor,
        tool,
        action="fail",
        key="serial_novel.chapter",
        step="publish",
        idempotency_key="chapter:7:publish",
        expected_revision=3,
        claim_token=publish_claim.data["claim_token"],
        error="目标服务暂时不可用",
    )
    assert failed.success is True
    workflow = failed.data["workflow"]
    assert workflow["status"] == "failed"
    assert workflow["last_success"]["step"] == "write"
    assert workflow["last_error"]["step"] == "publish"

    wrong_key = _run(
        executor,
        tool,
        action="fail",
        key="serial_novel.chapter",
        step="publish",
        idempotency_key="chapter:7:other",
        expected_revision=4,
        claim_token=publish_claim.data["claim_token"],
        error="不应覆盖",
    )
    assert wrong_key.success is False
    assert wrong_key.data["conflict"] is True

    resumed = _run(executor, tool, action="resume", key="serial_novel.chapter")
    assert resumed.data["next_step"] == "publish"

    reclaimed = _run(
        executor,
        tool,
        action="claim",
        key="serial_novel.chapter",
        step="publish",
        tool_name="group_file_exec",
        idempotency_key="chapter:7:publish",
        expected_revision=4,
    )
    assert reclaimed.success is True
    assert reclaimed.data["claimed"] is True
    assert reclaimed.data["workflow"]["steps"]["publish"]["claim_count"] == 2


def test_workflow_state_version_and_revision_conflicts_do_not_overwrite(tmp_path: Path):
    tool = _tool(tmp_path)
    executor = _executor(tmp_path)
    created = _run(executor, tool, action="begin", key="serial_novel.chapter", version="1")

    bad_version = _run(executor, tool, action="resume", key="serial_novel.chapter", version="2")
    assert bad_version.success is False
    assert bad_version.data["conflict"] is True

    bad_revision = _run(
        executor,
        tool,
        action="claim",
        key="serial_novel.chapter",
        step="write",
        tool_name="bash",
        idempotency_key="chapter:7:write",
        expected_revision=99,
    )
    assert bad_revision.success is False
    assert bad_revision.data["conflict"] is True

    current = _run(executor, tool, action="resume", key="serial_novel.chapter")
    assert current.success is True
    assert current.data["workflow"]["revision"] == created.data["workflow"]["revision"] == 0


def test_workflow_state_reads_legacy_schema_and_migrates_on_write(tmp_path: Path):
    tool = _tool(tmp_path)
    executor = _executor(tmp_path)
    created = _run(
        executor,
        tool,
        action="begin",
        key="serial_novel.chapter",
        state_json=json.dumps({"chapter": 7}),
    )
    store = executor.get_data_store("workflow_state")
    storage_key = next(key for key in store.keys() if key.endswith(":serial_novel.chapter"))
    legacy = store.get(storage_key)
    legacy["schema_version"] = 1
    legacy["events"] = [{"summary": "legacy event", "token": "must-not-leak"}]
    store.set(storage_key, legacy)
    store.save()

    resumed = _run(executor, tool, action="resume", key="serial_novel.chapter")
    assert resumed.success is True
    assert resumed.data["workflow"]["schema_version"] == 2
    assert "must-not-leak" not in json.dumps(resumed.data, ensure_ascii=False)

    claimed = _run(
        executor,
        tool,
        action="claim",
        key="serial_novel.chapter",
        step="write",
        tool_name="bash",
        idempotency_key="chapter:7:write",
        expected_revision=created.data["revision"],
    )
    assert claimed.success is True
    assert store.get(storage_key)["schema_version"] == 2


def test_workflow_state_restart_starts_a_new_run_without_clear(tmp_path: Path):
    tool = _tool(tmp_path)
    executor = _executor(tmp_path)
    _run(executor, tool, action="begin", key="napcat.like", state_json='{"user_id": 123}')
    claim = _run(
        executor,
        tool,
        action="claim",
        key="napcat.like",
        step="like",
        tool_name="qq_like",
        idempotency_key="like:123:1",
        expected_revision=0,
    )
    _run(
        executor,
        tool,
        action="checkpoint",
        key="napcat.like",
        step="like",
        idempotency_key="like:123:1",
        expected_revision=1,
        claim_token=claim.data["claim_token"],
    )
    completed = _run(
        executor,
        tool,
        action="checkpoint",
        key="napcat.like",
        step="like",
        idempotency_key="like:123:1",
        expected_revision=1,
        claim_token=claim.data["claim_token"],
    )
    old = completed.data["workflow"]

    restarted = _run(
        executor,
        tool,
        action="restart",
        key="napcat.like",
        expected_revision=2,
        state_json='{"user_id": 456}',
    )
    assert restarted.success is True
    new = restarted.data["workflow"]
    assert new["workflow_id"] != old["workflow_id"]
    assert new["previous_workflow_id"] == old["workflow_id"]
    assert new["run_number"] == 2
    assert new["inputs"] == {"user_id": 456}
    assert new["history"][-1]["workflow_id"] == old["workflow_id"]
    assert new["history"][-1]["status"] == "completed"


def test_workflow_state_is_chat_scoped_and_validates_payloads(tmp_path: Path):
    tool = _tool(tmp_path)
    executor = _executor(tmp_path)
    saved = _run(
        executor,
        tool,
        action="begin",
        key="serial_novel.chapter",
        state_json=json.dumps(
            {
                "chapter": 7,
                "target": "draft.md",
                "token": "secret",
                "access_token": "secret-2",
                "client_secret": "secret-3",
                "nested": {"password": "x"},
            }
        ),
    )
    assert saved.success is True
    assert saved.data["workflow"]["inputs"] == {
        "chapter": 7,
        "target": "draft.md",
        "nested": {},
    }

    other_chat = _executor(tmp_path, group_id="group-2")
    missing = _run(other_chat, tool, action="resume", key="serial_novel.chapter")
    assert missing.success is True
    assert missing.data["found"] is False

    bad_json = _run(executor, tool, action="begin", key="bad", state_json="[]")
    _run(executor, tool, action="begin", key="bad")
    bad_key = _run(executor, tool, action="resume", key="../secret")
    missing_idem = _run(executor, tool, action="claim", key="bad", step="step", expected_revision=0)
    short_lease = _run(
        executor,
        tool,
        action="claim",
        key="bad",
        step="step",
        idempotency_key="bad:step",
        lease_seconds=29,
        expected_revision=0,
    )

    assert bad_json.success is False
    assert "JSON 对象" in bad_json.error
    assert bad_key.success is False
    assert "路径分隔符" in bad_key.error
    assert missing_idem.success is False
    assert "idempotency_key" in missing_idem.error
    assert short_lease.success is False
    assert "lease_seconds" in short_lease.error
