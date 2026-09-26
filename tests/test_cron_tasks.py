from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from sirius_pulse.memory.user.unified_models import UnifiedUser
from sirius_pulse.tools import cron_tasks
from sirius_pulse.tools.builtin import bash
from sirius_pulse.tools.builtin._internal import _bash_cron
from sirius_pulse.tools.models import ToolInvocationContext


def test_parse_echo_crontab_install_command():
    request = cron_tasks.parse_crontab_command("echo '*/5 * * * * echo hello' | crontab -")

    assert request == {
        "action": "install",
        "entries": [{"expression": "*/5 * * * *", "command": "echo hello"}],
    }


def test_parse_printf_crontab_install_command():
    request = cron_tasks.parse_crontab_command(
        "printf '%s\\n' '0 8 * * 1-5 echo weekday' | crontab -"
    )

    assert request["entries"][0]["expression"] == "0 8 * * 1-5"


def test_parse_crontab_list_and_remove():
    assert cron_tasks.parse_crontab_command("crontab -l") == {"action": "list"}
    assert cron_tasks.parse_crontab_command("crontab -r") == {"action": "remove"}


def test_parse_crontab_list_with_redirect_and_status_probe():
    assert cron_tasks.parse_crontab_command('crontab -l 2>/dev/null; echo "---exit: $?---"') == {
        "action": "list"
    }


def test_parse_crontab_install_with_follow_up_list():
    request = cron_tasks.parse_crontab_command(
        "printf '%s\\n' '0 9 * * * echo reminder' | crontab - && crontab -l"
    )

    assert request["entries"] == [{"expression": "0 9 * * *", "command": "echo reminder"}]


def test_parse_printf_literal_with_newline_escape():
    request = cron_tasks.parse_crontab_command("printf '0 9 * * * echo reminder\\n' | crontab -")

    assert request["entries"] == [{"expression": "0 9 * * *", "command": "echo reminder"}]


def test_cron_matches_common_fields_and_or_day_semantics():
    monday = datetime(2026, 8, 10, 8, 0, tzinfo=cron_tasks.CRON_TIMEZONE)
    assert cron_tasks.cron_matches("0 8 * * 1-5", monday)
    assert cron_tasks.cron_matches("0 8 10 * 0", monday)
    assert not cron_tasks.cron_matches("1 8 * * 1-5", monday)


def test_cron_matches_sunday_as_zero_or_seven():
    sunday = datetime(2026, 8, 9, 8, 0, tzinfo=cron_tasks.CRON_TIMEZONE)

    assert cron_tasks.cron_matches("0 8 * * 0", sunday)
    assert cron_tasks.cron_matches("0 8 * * 7", sunday)


def test_cron_day_or_rule_treats_step_wildcards_as_restricted():
    odd_day = datetime(2026, 8, 11, 8, 0, tzinfo=cron_tasks.CRON_TIMEZONE)
    even_day = datetime(2026, 8, 10, 8, 0, tzinfo=cron_tasks.CRON_TIMEZONE)

    assert cron_tasks.cron_matches("0 8 */2 * *", odd_day) is True
    assert cron_tasks.cron_matches("0 8 */2 * 0", even_day) is False


def test_cron_rejects_seconds_field():
    with pytest.raises(cron_tasks.CronParseError, match="五字段"):
        cron_tasks.validate_expression("0 0 8 * * *")


def test_crontab_text_does_not_intercept_an_unrelated_shell_command():
    assert cron_tasks.parse_crontab_command("echo crontab") is None


class _CronStore:
    def __init__(self):
        self.data = {}
        self.saved = 0

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value

    def save(self):
        self.saved += 1


@pytest.mark.asyncio
async def test_bash_crontab_registers_lists_and_removes_jobs(tmp_path):
    store = _CronStore()
    invocation = ToolInvocationContext(
        caller=UnifiedUser(user_id="u1", name="Alice", metadata={"is_developer": False})
    )
    context = {"group_id": "group-1", "user_id": "u1", "adapter_type": "napcat"}

    installed = await bash.run(
        "echo '*/5 * * * * echo hello' | crontab -",
        cwd=str(tmp_path),
        data_store=store,
        chat_context=context,
        invocation_context=invocation,
    )
    listed = await bash.run(
        "crontab -l",
        data_store=store,
        chat_context=context,
        invocation_context=invocation,
    )
    removed = await bash.run(
        "crontab -r",
        data_store=store,
        chat_context=context,
        invocation_context=invocation,
    )

    assert installed["success"] is True
    assert listed["success"] is True
    assert "*/5 * * * * echo hello" in listed["text_blocks"][0]
    assert removed["success"] is True
    assert store.data["cron_jobs"] == []


@pytest.mark.asyncio
async def test_bash_crontab_stdin_replaces_the_current_chat_crontab(tmp_path):
    store = _CronStore()
    context = {"group_id": "group-1", "adapter_type": "napcat"}
    invocation = ToolInvocationContext(caller=UnifiedUser(user_id="u1", name="Alice", metadata={}))

    await bash.run(
        "echo '0 8 * * * echo old' | crontab -",
        cwd=str(tmp_path),
        data_store=store,
        chat_context=context,
        invocation_context=invocation,
    )
    await bash.run(
        "echo '30 9 * * * echo new' | crontab -",
        cwd=str(tmp_path),
        data_store=store,
        chat_context=context,
        invocation_context=invocation,
    )

    assert len(store.data["cron_jobs"]) == 1
    assert store.data["cron_jobs"][0]["command"] == "echo new"


@pytest.mark.asyncio
async def test_recurring_cron_occurrences_use_distinct_proactive_event_ids():
    dispatched: list[str] = []

    async def run_command(*_args, **_kwargs):
        return {"success": True, "text_blocks": ["ok"]}

    async def generate_scheduled_message(**_kwargs):
        return {"text": "定时通知"}

    async def dispatch_proactive_message(**kwargs):
        dispatched.append(kwargs["event_id"])
        return True

    ctx = SimpleNamespace(
        get_data_store=lambda _name: _CronStore(),
        generate_scheduled_message=generate_scheduled_message,
        dispatch_proactive_message=dispatch_proactive_message,
    )
    job = {
        "id": "cron-stable",
        "command": "echo ok",
        "group_id": "100",
        "owner_user_id": "u1",
        "owner_name": "Alice",
        "adapter_type": "napcat",
    }

    await _bash_cron._run_job(ctx, job, run_command, run_key="2026-08-10T08:00")
    await _bash_cron._run_job(ctx, job, run_command, run_key="2026-08-10T08:05")

    assert dispatched == [
        "cron-stable:2026-08-10T08:00",
        "cron-stable:2026-08-10T08:05",
    ]


@pytest.mark.asyncio
async def test_failed_cron_delivery_retries_same_occurrence_before_advancing(monkeypatch):
    store = _CronStore()
    store.data["cron_jobs"] = [
        {
            "id": "cron-retry",
            "expression": "* * * * *",
            "command": "echo retry",
            "group_id": "100",
            "owner_user_id": "u1",
            "owner_name": "Alice",
            "adapter_type": "napcat",
            "last_run_key": "",
            "run_count": 0,
        }
    ]
    event_ids: list[str] = []
    command_runs = 0
    message_generations = 0
    outcomes = iter([False, True])
    _bash_cron._pending_payloads.clear()

    monkeypatch.setattr(
        cron_tasks,
        "task_is_due",
        lambda _job, _now: (True, "cron:202608100800"),
    )

    async def run_command(*_args, **_kwargs):
        nonlocal command_runs
        command_runs += 1
        return {"success": True, "text_blocks": ["ok"]}

    async def generate_scheduled_message(**_kwargs):
        nonlocal message_generations
        message_generations += 1
        return {"text": "定时通知"}

    async def dispatch_proactive_message(**kwargs):
        event_ids.append(kwargs["event_id"])
        return next(outcomes)

    ctx = SimpleNamespace(
        get_data_store=lambda _name: store,
        generate_scheduled_message=generate_scheduled_message,
        dispatch_proactive_message=dispatch_proactive_message,
    )

    await _bash_cron.check_tasks(ctx, run_command)
    failed = store.data["cron_jobs"][0]
    assert failed["last_run_key"] == ""
    assert failed["pending_run_key"] == "cron:202608100800"
    assert failed["run_count"] == 0

    await _bash_cron.check_tasks(ctx, run_command)
    delivered = store.data["cron_jobs"][0]
    assert delivered["last_run_key"] == "cron:202608100800"
    assert "pending_run_key" not in delivered
    assert delivered["run_count"] == 1
    assert command_runs == 1
    assert message_generations == 1
    assert _bash_cron._pending_payloads == {}
    assert event_ids == [
        "cron-retry:cron:202608100800",
        "cron-retry:cron:202608100800",
    ]


@pytest.mark.asyncio
async def test_cron_restart_does_not_repeat_an_ambiguously_started_command(monkeypatch):
    store = _CronStore()
    store.data["cron_jobs"] = [
        {
            "id": "cron-restart",
            "expression": "* * * * *",
            "command": "touch external-side-effect",
            "group_id": "100",
            "owner_user_id": "u1",
            "owner_name": "Alice",
            "adapter_type": "napcat",
            "last_run_key": "",
            "run_count": 0,
        }
    ]
    command_runs = 0
    dispatches = 0
    _bash_cron._pending_payloads.clear()
    _bash_cron._pending_payload_locks.clear()
    monkeypatch.setattr(
        cron_tasks,
        "task_is_due",
        lambda _job, _now: (True, "cron:202608100800"),
    )

    async def run_command(*_args, **_kwargs):
        nonlocal command_runs
        command_runs += 1
        return {"success": True, "text_blocks": ["created"]}

    async def generate_scheduled_message(**_kwargs):
        return {"text": "定时通知"}

    async def dispatch_proactive_message(**_kwargs):
        nonlocal dispatches
        dispatches += 1
        return False

    ctx = SimpleNamespace(
        get_data_store=lambda _name: store,
        generate_scheduled_message=generate_scheduled_message,
        dispatch_proactive_message=dispatch_proactive_message,
    )

    await _bash_cron.check_tasks(ctx, run_command)
    assert command_runs == 1
    assert dispatches == 1
    assert store.data["cron_jobs"][0]["pending_execution_started_key"] == ("cron:202608100800")

    # Simulate process restart: command-derived payload is intentionally not
    # persisted because it may contain credentials.
    _bash_cron._pending_payloads.clear()
    _bash_cron._pending_payload_locks.clear()
    await _bash_cron.check_tasks(ctx, run_command)

    job = store.data["cron_jobs"][0]
    assert command_runs == 1
    assert dispatches == 1
    assert job["last_run_key"] == "cron:202608100800"
    assert job["run_count"] == 1
    assert "pending_run_key" not in job
    assert "pending_execution_started_key" not in job


def test_cron_rejects_unsupported_expression():
    try:
        cron_tasks.validate_expression("@daily")
    except cron_tasks.CronParseError:
        pass
    else:
        raise AssertionError("@daily should be rejected by the five-field parser")
