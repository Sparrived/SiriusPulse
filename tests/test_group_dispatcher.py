from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sirius_pulse.core.group_dispatcher import GroupDispatcher


class _Clock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _dispatcher(
    path: Path,
    clock: _Clock,
    worker_id: str,
    account_id: str,
    peer_cooldown_seconds: float = 5,
    **kwargs,
) -> GroupDispatcher:
    return GroupDispatcher(
        path,
        worker_id=worker_id,
        account_id=account_id,
        clock=clock,
        min_reply_interval_seconds=3,
        peer_cooldown_seconds=peer_cooldown_seconds,
        **kwargs,
    )


def test_group_dispatcher_selects_one_worker_and_releases_turn(tmp_path: Path):
    clock = _Clock()
    db_path = tmp_path / "dispatcher.db"
    first = _dispatcher(db_path, clock, "alpha", "100")
    second = _dispatcher(db_path, clock, "beta", "200")

    granted = first.admit(event_id="m1", group_id="g1")
    observed = second.admit(event_id="m1", group_id="g1")

    assert granted.granted
    assert observed.action == "observe"
    assert observed.reason == "event_granted"

    assert first.finish(granted.lease_id, sent=True)
    clock.value += 4
    next_turn = second.admit(event_id="m2", group_id="g1")
    assert next_turn.granted
    assert next_turn.worker_id == "beta"


def test_group_dispatcher_retries_failed_reminders_but_deduplicates_sent_ones(
    tmp_path: Path,
):
    clock = _Clock()
    dispatcher = _dispatcher(tmp_path / "dispatcher.db", clock, "alpha", "100")
    event_id = "reminder:g1:sub2api-event"

    first = dispatcher.admit(
        event_id=event_id,
        group_id="g1",
        sender_type="system",
        preferred_worker_id="alpha",
    )
    assert first.granted
    in_flight_duplicate = dispatcher.admit(
        event_id=event_id,
        group_id="g1",
        sender_type="system",
        preferred_worker_id="alpha",
    )
    assert not in_flight_duplicate.granted
    assert in_flight_duplicate.reason == "event_granted"
    assert dispatcher.finish(first.lease_id, sent=False)

    retry = dispatcher.admit(
        event_id=event_id,
        group_id="g1",
        sender_type="system",
        preferred_worker_id="alpha",
    )
    assert retry.granted
    assert retry.lease_id != first.lease_id
    assert dispatcher.finish(retry.lease_id, sent=True)

    duplicate = dispatcher.admit(
        event_id=event_id,
        group_id="g1",
        sender_type="system",
        preferred_worker_id="alpha",
    )
    assert not duplicate.granted
    assert duplicate.reason == "event_sent"


def test_dispatcher_close_preserves_event_until_normal_lease_expiry(tmp_path: Path):
    clock = _Clock()
    db_path = tmp_path / "dispatcher.db"
    dispatcher = _dispatcher(db_path, clock, "alpha", "100", lease_seconds=5)

    granted = dispatcher.admit(
        event_id="reminder:g1:close-before-send",
        group_id="g1",
        sender_type="system",
        preferred_worker_id="alpha",
    )
    assert granted.granted

    dispatcher.close()
    before_expiry = dispatcher.admit(
        event_id="reminder:g1:close-before-send",
        group_id="g1",
        sender_type="system",
        preferred_worker_id="alpha",
    )
    assert before_expiry.granted is False
    assert before_expiry.reason == "event_granted"

    clock.value += 6
    retry = dispatcher.admit(
        event_id="reminder:g1:close-before-send",
        group_id="g1",
        sender_type="system",
        preferred_worker_id="alpha",
    )
    assert retry.granted
    assert retry.lease_id != granted.lease_id


def test_reminder_lease_expiry_retries_only_before_platform_delivery_starts(
    tmp_path: Path,
):
    clock = _Clock()
    db_path = tmp_path / "dispatcher.db"
    dispatcher = _dispatcher(db_path, clock, "alpha", "100", lease_seconds=5)

    safe = dispatcher.admit(
        event_id="reminder:g1:not-started",
        group_id="g1",
        sender_type="system",
        preferred_worker_id="alpha",
    )
    assert safe.granted
    clock.value += 6
    safe_retry = dispatcher.admit(
        event_id="reminder:g1:not-started",
        group_id="g1",
        sender_type="system",
        preferred_worker_id="alpha",
    )
    assert safe_retry.granted
    assert safe_retry.lease_id != safe.lease_id
    assert dispatcher.finish(safe_retry.lease_id, sent=True)

    started = dispatcher.admit(
        event_id="reminder:g2:started",
        group_id="g2",
        sender_type="system",
        preferred_worker_id="alpha",
    )
    assert started.granted
    assert dispatcher.begin_delivery(started.lease_id)
    clock.value += 6
    blocked_replay = dispatcher.admit(
        event_id="reminder:g2:started",
        group_id="g2",
        sender_type="system",
        preferred_worker_id="alpha",
    )
    assert not blocked_replay.granted
    assert blocked_replay.reason == "event_uncertain"
    assert not dispatcher.finish(started.lease_id, sent=True)


def test_expired_sending_is_made_uncertain_before_another_group_prunes_events(
    tmp_path: Path,
):
    clock = _Clock()
    dispatcher = _dispatcher(
        tmp_path / "dispatcher.db",
        clock,
        "alpha",
        "100",
        lease_seconds=5,
    )
    event_id = "reminder:g1:cross-group-prune"
    started = dispatcher.admit(
        event_id=event_id,
        group_id="g1",
        sender_type="system",
        preferred_worker_id="alpha",
    )
    assert started.granted
    assert dispatcher.begin_delivery(started.lease_id)

    clock.value += 86400 + 10
    other_group = dispatcher.admit(event_id="ordinary:g2", group_id="g2")
    assert other_group.granted

    blocked_replay = dispatcher.admit(
        event_id=event_id,
        group_id="g1",
        sender_type="system",
        preferred_worker_id="alpha",
    )
    assert blocked_replay.granted is False
    assert blocked_replay.reason == "event_uncertain"


def test_terminal_reminder_receipt_survives_event_age_and_volume_pruning(tmp_path: Path):
    clock = _Clock()
    dispatcher = _dispatcher(tmp_path / "dispatcher.db", clock, "alpha", "100")
    event_id = "reminder:g1:durable-receipt"
    decision = dispatcher.admit(
        event_id=event_id,
        group_id="g1",
        sender_type="system",
        preferred_worker_id="alpha",
    )
    assert decision.granted
    assert dispatcher.finish(decision.lease_id, sent=True)

    with dispatcher._connect() as conn:
        conn.executemany(
            """
            INSERT INTO dispatcher_events(
                event_id, group_id, worker_id, lease_id, status, reason,
                created_at, updated_at
            ) VALUES (?, 'volume', 'alpha', '', 'sent', '', ?, ?)
            """,
            [
                (f"ordinary-volume-{index}", clock.value + index + 1, clock.value + index + 1)
                for index in range(65_537)
            ],
        )

    clock.value += 65_540
    volume_trigger = dispatcher.admit(event_id="ordinary:volume-trigger", group_id="volume")
    assert volume_trigger.granted
    assert dispatcher.finish(volume_trigger.lease_id, sent=False)
    volume_duplicate = dispatcher.admit(
        event_id=event_id,
        group_id="g1",
        sender_type="system",
        preferred_worker_id="alpha",
    )
    assert volume_duplicate.reason == "event_sent"

    clock.value += 31 * 86400
    age_trigger = dispatcher.admit(event_id="ordinary:age-trigger", group_id="age")
    assert age_trigger.granted
    age_duplicate = dispatcher.admit(
        event_id=event_id,
        group_id="g1",
        sender_type="system",
        preferred_worker_id="alpha",
    )
    assert age_duplicate.granted is False
    assert age_duplicate.reason == "event_sent"


def test_reminder_delivery_lease_can_be_renewed_during_slow_io(tmp_path: Path):
    clock = _Clock()
    dispatcher = _dispatcher(
        tmp_path / "dispatcher.db",
        clock,
        "alpha",
        "100",
        lease_seconds=5,
    )
    event_id = "reminder:g1:slow-delivery"
    decision = dispatcher.admit(
        event_id=event_id,
        group_id="g1",
        sender_type="system",
        preferred_worker_id="alpha",
    )
    assert decision.granted
    assert dispatcher.begin_delivery(decision.lease_id)

    clock.value += 4
    assert dispatcher.renew(decision.lease_id)
    clock.value += 4
    duplicate = dispatcher.admit(
        event_id=event_id,
        group_id="g1",
        sender_type="system",
        preferred_worker_id="alpha",
    )
    assert not duplicate.granted
    assert duplicate.reason == "event_sending"
    assert dispatcher.finish(decision.lease_id, sent=True)


def test_group_dispatcher_targeted_account_wins_and_bypasses_cooldown(tmp_path: Path):
    clock = _Clock()
    db_path = tmp_path / "dispatcher.db"
    first = _dispatcher(db_path, clock, "alpha", "100")
    second = _dispatcher(db_path, clock, "beta", "200")

    first_turn = first.admit(event_id="m1", group_id="g1")
    assert first_turn.granted
    assert first.finish(first_turn.lease_id, sent=True)

    blocked = first.admit(event_id="m2", group_id="g1")
    assert blocked.action == "observe"
    assert blocked.reason == "reply_cooldown"

    targeted = second.admit(
        event_id="m3",
        group_id="g1",
        target_account_ids=("200",),
    )
    assert targeted.granted
    assert targeted.worker_id == "beta"


def test_group_dispatcher_limits_peer_turns_until_humans_return(tmp_path: Path):
    clock = _Clock()
    db_path = tmp_path / "dispatcher.db"
    first = _dispatcher(db_path, clock, "alpha", "100", max_peer_turns=1)
    second = _dispatcher(db_path, clock, "beta", "200", max_peer_turns=1)

    human = first.admit(event_id="m1", group_id="g1")
    assert human.granted
    assert first.finish(human.lease_id, sent=True)

    clock.value += 61
    peer = second.admit(
        event_id="m2",
        group_id="g1",
        sender_type="other_ai",
        sender_account_id="100",
        target_account_ids=("200",),
    )
    assert peer.granted
    assert second.finish(peer.lease_id, sent=True)

    clock.value += 1
    another_peer = first.admit(
        event_id="m3",
        group_id="g1",
        sender_type="other_ai",
        sender_account_id="200",
        target_account_ids=("100",),
    )
    assert another_peer.action == "observe"
    assert another_peer.reason == "peer_budget_exhausted"


def test_group_dispatcher_opens_targeted_peer_loop_without_preview_score(
    tmp_path: Path,
):
    async def run() -> None:
        clock = _Clock()
        db_path = tmp_path / "dispatcher.db"
        first = _dispatcher(db_path, clock, "alpha", "100")
        second = _dispatcher(db_path, clock, "beta", "200")

        human = await first.coordinate(
            event_id="m1",
            group_id="g1",
            base_score=1.0,
            should_reply=True,
            message_text="这个问题怎么看？",
        )
        assert human.granted
        assert first.finish(
            human.lease_id,
            sent=True,
            response_text="beta，你怎么看这个问题？",
        )

        clock.value += 6
        peer = await second.coordinate(
            event_id="m2",
            group_id="g1",
            base_score=0.0,
            should_reply=False,
            sender_type="other_ai",
            sender_account_id="100",
            message_text="我觉得这个问题需要从实际情况看。",
        )
        assert peer.granted
        assert second.finish(peer.lease_id, sent=True, response_text="我同意这个判断。")

    asyncio.run(run())


def test_group_dispatcher_does_not_open_peer_loop_after_targeted_human_turn(
    tmp_path: Path,
):
    async def run() -> None:
        clock = _Clock()
        db_path = tmp_path / "dispatcher.db"
        first = _dispatcher(db_path, clock, "alpha", "100")
        second = _dispatcher(db_path, clock, "beta", "200")

        alpha = await first.coordinate(
            event_id="targeted-human-1",
            group_id="g1",
            base_score=1.0,
            should_reply=True,
            preferred_worker_id="alpha",
            message_text="月白，帮我看看这个问题？",
        )
        assert alpha.granted
        assert first.finish(
            alpha.lease_id,
            sent=True,
            response_text="我在呢，日暮也可以一起看看这个问题？",
        )

        clock.value += 6
        peer = await second.coordinate(
            event_id="targeted-human-2",
            group_id="g1",
            base_score=1.0,
            should_reply=True,
            sender_type="other_ai",
            sender_account_id="100",
            message_text="这个问题要怎么处理？",
        )
        assert peer.action == "observe"
        assert peer.reason == "peer_topic_closed"

    asyncio.run(run())


def test_group_dispatcher_prefers_immediate_and_forces_one_peer_turn(
    tmp_path: Path,
):
    async def run() -> None:
        clock = _Clock()
        db_path = tmp_path / "dispatcher.db"
        delayed = _dispatcher(db_path, clock, "alpha", "100")
        immediate = _dispatcher(db_path, clock, "beta", "200")

        alpha, beta = await asyncio.gather(
            delayed.coordinate(
                event_id="multi-reply-1",
                group_id="g1",
                base_score=5.0,
                should_reply=True,
                response_strategy="delayed",
                response_delay_seconds=12.0,
            ),
            immediate.coordinate(
                event_id="multi-reply-1",
                group_id="g1",
                base_score=0.2,
                should_reply=True,
                response_strategy="immediate",
            ),
        )
        assert not alpha.granted
        assert beta.granted
        assert beta.response_strategy == "immediate"
        assert immediate.finish(beta.lease_id, sent=True, response_text="我先回答这个问题。")

        clock.value += 6
        peer = await delayed.coordinate(
            event_id="multi-reply-2",
            group_id="g1",
            base_score=0.0,
            should_reply=False,
            response_strategy="silent",
            sender_type="other_ai",
            sender_account_id="200",
            message_text="我也补充一下这个问题。",
        )
        assert peer.granted
        assert delayed.finish(peer.lease_id, sent=True, response_text="补充完成。")

    asyncio.run(run())


def test_group_dispatcher_single_reply_uses_existing_peer_trigger_only(
    tmp_path: Path,
):
    async def run() -> None:
        clock = _Clock()
        db_path = tmp_path / "dispatcher.db"
        silent = _dispatcher(db_path, clock, "alpha", "100")
        delayed = _dispatcher(db_path, clock, "beta", "200")

        alpha, beta = await asyncio.gather(
            silent.coordinate(
                event_id="single-reply-1",
                group_id="g1",
                base_score=0.0,
                should_reply=False,
                response_strategy="silent",
            ),
            delayed.coordinate(
                event_id="single-reply-1",
                group_id="g1",
                base_score=1.0,
                should_reply=True,
                response_strategy="delayed",
                response_delay_seconds=12.0,
            ),
        )
        assert not alpha.granted
        assert beta.granted
        assert beta.response_strategy == "delayed"
        assert delayed.finish(beta.lease_id, sent=True, response_text="我先说我的看法。")

        clock.value += 6
        peer = await silent.coordinate(
            event_id="single-reply-2",
            group_id="g1",
            base_score=1.0,
            should_reply=True,
            sender_type="other_ai",
            sender_account_id="200",
            message_text="我也补充一下。",
        )
        assert peer.action == "observe"
        assert peer.reason == "peer_topic_closed"

    asyncio.run(run())


def test_group_dispatcher_keeps_peer_closed_when_all_human_candidates_are_silent(
    tmp_path: Path,
):
    async def run() -> None:
        clock = _Clock()
        db_path = tmp_path / "dispatcher.db"
        first = _dispatcher(db_path, clock, "alpha", "100")
        second = _dispatcher(db_path, clock, "beta", "200")

        alpha, beta = await asyncio.gather(
            first.coordinate(
                event_id="silent-human-1",
                group_id="g1",
                base_score=0.0,
                should_reply=False,
                response_strategy="silent",
            ),
            second.coordinate(
                event_id="silent-human-1",
                group_id="g1",
                base_score=0.0,
                should_reply=False,
                response_strategy="silent",
            ),
        )
        assert alpha.reason == "all_candidates_silent"
        assert beta.reason == "all_candidates_silent"

        clock.value += 6
        peer = await first.coordinate(
            event_id="silent-human-2",
            group_id="g1",
            base_score=1.0,
            should_reply=True,
            sender_type="other_ai",
            sender_account_id="200",
            message_text="我也想补充一下。",
        )
        assert peer.reason == "peer_topic_closed"

    asyncio.run(run())


def test_group_dispatcher_does_not_override_silent_persona_for_open_topic(tmp_path: Path):
    async def run() -> None:
        clock = _Clock()
        db_path = tmp_path / "dispatcher.db"
        first = _dispatcher(db_path, clock, "alpha", "100")
        second = _dispatcher(db_path, clock, "beta", "200")

        human = await first.coordinate(
            event_id="m1",
            group_id="g1",
            base_score=1.0,
            should_reply=True,
            message_text="夜拍参数怎么设置？",
        )
        assert human.granted
        assert first.finish(
            human.lease_id,
            sent=True,
            response_text="夜拍参数应该优先调整什么？",
        )

        clock.value += 6
        peer = await second.coordinate(
            event_id="m2",
            group_id="g1",
            base_score=0.0,
            should_reply=False,
            sender_type="other_ai",
            sender_account_id="100",
            message_text="夜拍参数先调快门还是 ISO？",
        )
        assert peer.reason == "all_candidates_silent"

    asyncio.run(run())


def test_group_dispatcher_rejects_peer_message_without_open_topic(tmp_path: Path):
    clock = _Clock()
    db_path = tmp_path / "dispatcher.db"
    first = _dispatcher(db_path, clock, "alpha", "100")
    second = _dispatcher(db_path, clock, "beta", "200")

    human = first.admit(event_id="m1", group_id="g1")
    assert human.granted
    assert first.finish(human.lease_id, sent=True, response_text="今天晚饭吃面。")

    clock.value += 61
    peer = second.admit(
        event_id="m2",
        group_id="g1",
        sender_type="other_ai",
        sender_account_id="100",
        message_text="今天晚饭吃什么？",
    )
    assert peer.reason == "peer_topic_closed"


def test_group_dispatcher_defers_peer_event_until_active_turn_releases(tmp_path: Path):
    async def run() -> None:
        clock = _Clock()
        db_path = tmp_path / "dispatcher.db"
        first = _dispatcher(db_path, clock, "alpha", "100", peer_cooldown_seconds=0)
        second = _dispatcher(db_path, clock, "beta", "200", peer_cooldown_seconds=0)

        human = first.admit(event_id="m1", group_id="g1")
        assert human.granted

        pending = await second.coordinate(
            event_id="m2",
            group_id="g1",
            base_score=1.0,
            should_reply=True,
            sender_type="other_ai",
            sender_account_id="100",
            target_account_ids=("200",),
        )
        assert pending.deferred
        assert pending.reason == "group_busy"

        assert first.finish(human.lease_id, sent=True)
        resumed = await second.coordinate(
            event_id="m2",
            group_id="g1",
            base_score=1.0,
            should_reply=True,
            sender_type="other_ai",
            sender_account_id="100",
            target_account_ids=("200",),
        )
        assert resumed.granted

    asyncio.run(run())


def test_group_dispatcher_expired_lease_does_not_block_group(tmp_path: Path):
    clock = _Clock()
    db_path = tmp_path / "dispatcher.db"
    first = _dispatcher(db_path, clock, "alpha", "100", lease_seconds=5)
    second = _dispatcher(db_path, clock, "beta", "200", lease_seconds=5)

    turn = first.admit(event_id="m1", group_id="g1")
    assert turn.granted
    clock.value += 6

    replacement = second.admit(event_id="m2", group_id="g1")
    assert replacement.action == "observe"
    assert first.admit(event_id="m2", group_id="g1").granted


def test_group_dispatcher_read_snapshot_does_not_register_a_webui_worker(tmp_path: Path):
    clock = _Clock()
    db_path = tmp_path / "dispatcher.db"
    first = _dispatcher(db_path, clock, "alpha", "100")
    second = _dispatcher(db_path, clock, "beta", "200")

    turn = first.admit(event_id="m1", group_id="g1")
    assert turn.granted
    assert first.finish(turn.lease_id, sent=True)

    snapshot = GroupDispatcher.read_snapshot(db_path, now=clock.value)

    assert snapshot["available"] is True
    assert {worker["worker_id"] for worker in snapshot["workers"]} == {"alpha", "beta"}
    assert snapshot["summary"]["sent_24h"] == 1
    assert snapshot["events"][0]["reason"] == "selected"
    assert all(worker["worker_id"] != "webui" for worker in snapshot["workers"])
    second.close()


def test_group_dispatcher_final_score_selects_highest_candidate(tmp_path: Path):
    async def run() -> None:
        clock = _Clock()
        db_path = tmp_path / "dispatcher.db"
        first = _dispatcher(db_path, clock, "alpha", "100")
        second = _dispatcher(db_path, clock, "beta", "200")

        alpha, beta = await asyncio.gather(
            first.coordinate(
                event_id="scored-1",
                group_id="g1",
                base_score=0.55,
                should_reply=True,
            ),
            second.coordinate(
                event_id="scored-1",
                group_id="g1",
                base_score=0.90,
                should_reply=True,
            ),
        )
        assert not alpha.granted
        assert beta.granted
        assert beta.base_score == 0.9
        assert beta.final_score == 0.9

    asyncio.run(run())


def test_group_dispatcher_text_target_bonus_is_not_a_hard_filter(tmp_path: Path):
    async def run() -> None:
        clock = _Clock()
        db_path = tmp_path / "dispatcher.db"
        first = _dispatcher(db_path, clock, "alpha", "100")
        second = _dispatcher(db_path, clock, "beta", "200")

        alpha, beta = await asyncio.gather(
            first.coordinate(
                event_id="text-targeted-1",
                group_id="g1",
                base_score=1.2,
                should_reply=True,
            ),
            second.coordinate(
                event_id="text-targeted-1",
                group_id="g1",
                base_score=1.0,
                should_reply=True,
                preferred_worker_id="beta",
            ),
        )
        assert not alpha.granted
        assert beta.granted
        assert second.finish(beta.lease_id, sent=True)

        clock.value += 4
        alpha, beta = await asyncio.gather(
            first.coordinate(
                event_id="text-targeted-2",
                group_id="g2",
                base_score=3.0,
                should_reply=True,
            ),
            second.coordinate(
                event_id="text-targeted-2",
                group_id="g2",
                base_score=1.0,
                should_reply=True,
                preferred_worker_id="beta",
            ),
        )
        assert alpha.granted
        assert not beta.granted

    asyncio.run(run())


def test_group_dispatcher_penalizes_recently_active_worker(tmp_path: Path):
    async def run() -> None:
        clock = _Clock()
        db_path = tmp_path / "dispatcher.db"
        first = _dispatcher(db_path, clock, "alpha", "100")
        second = _dispatcher(db_path, clock, "beta", "200")

        alpha, beta = await asyncio.gather(
            first.coordinate(
                event_id="active-1",
                group_id="g1",
                base_score=0.90,
                should_reply=True,
            ),
            second.coordinate(
                event_id="active-1",
                group_id="g1",
                base_score=0.20,
                should_reply=True,
            ),
        )
        assert alpha.granted
        assert first.finish(alpha.lease_id, sent=True)

        clock.value += 4
        next_alpha, next_beta = await asyncio.gather(
            first.coordinate(
                event_id="active-2",
                group_id="g1",
                base_score=0.80,
                should_reply=True,
            ),
            second.coordinate(
                event_id="active-2",
                group_id="g1",
                base_score=0.75,
                should_reply=True,
            ),
        )
        assert not next_alpha.granted
        assert next_beta.granted
        assert next_beta.activity_penalty == 0.0

    asyncio.run(run())


@pytest.mark.parametrize(
    "addressing",
    [
        pytest.param({"target_account_ids": ("200",)}, id="at_mention"),
        pytest.param({"preferred_worker_id": "beta"}, id="text_name"),
    ],
)
def test_group_dispatcher_defers_addressed_message_while_group_is_busy(
    tmp_path: Path, addressing: dict
):
    """点名消息在群里已有发言时不能被静默丢弃，必须延后重试。

    回归：此前 reason=group_busy 会把点名消息直接 observe 掉，导致
    「带名字也不回」——线上约 40% 的点名消息是这样丢的。
    """

    async def run() -> None:
        clock = _Clock()
        db_path = tmp_path / "dispatcher.db"
        first = _dispatcher(db_path, clock, "alpha", "100")
        second = _dispatcher(db_path, clock, "beta", "200")

        busy = await first.coordinate(
            event_id="m1",
            group_id="g1",
            base_score=1.0,
            should_reply=True,
        )
        assert busy.granted

        named = await second.coordinate(
            event_id="m2",
            group_id="g1",
            base_score=1.0,
            should_reply=True,
            message_text="beta，你看这个怎么办？",
            **addressing,
        )
        assert named.deferred, "点名消息被静默丢弃了"
        assert named.reason == "group_busy"

        # 租约释放后，同一条消息重试必须能拿到发言权。
        assert first.finish(busy.lease_id, sent=True)
        resumed = await second.coordinate(
            event_id="m2",
            group_id="g1",
            base_score=1.0,
            should_reply=True,
            message_text="beta，你看这个怎么办？",
            **addressing,
        )
        assert resumed.granted
        assert resumed.worker_id == "beta"

    asyncio.run(run())


def test_group_dispatcher_observes_undirected_chatter_while_group_is_busy(tmp_path: Path):
    """未被点名的闲聊在群忙时保持静默，不因上面的修复变成全员排队。"""

    async def run() -> None:
        clock = _Clock()
        db_path = tmp_path / "dispatcher.db"
        first = _dispatcher(db_path, clock, "alpha", "100")
        second = _dispatcher(db_path, clock, "beta", "200")

        busy = await first.coordinate(
            event_id="m1",
            group_id="g1",
            base_score=1.0,
            should_reply=True,
        )
        assert busy.granted

        chatter = await second.coordinate(
            event_id="m2",
            group_id="g1",
            base_score=1.0,
            should_reply=True,
            message_text="今天天气不错。",
        )
        assert chatter.action == "observe"
        assert chatter.reason == "group_busy"

    asyncio.run(run())
