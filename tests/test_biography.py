"""Tests for biography system: models, store, manager, alias disambiguation."""

from __future__ import annotations

import tempfile
from unittest.mock import AsyncMock

import pytest

from sirius_chat.memory.biography.manager import BiographyManager, _build_update_prompt
from sirius_chat.memory.biography.models import AliasEntry, RelationshipAnchor, UserPersonaCard
from sirius_chat.memory.biography.store import BiographyStore


# ── Fixtures ────────────────────────────────────────────────────


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as tmp:
        yield BiographyStore(tmp)


@pytest.fixture
def manager():
    with tempfile.TemporaryDirectory() as tmp:
        yield BiographyManager(tmp)


# ── Models ──────────────────────────────────────────────────────


class TestUserPersonaCard:
    def test_default_card(self):
        card = UserPersonaCard(user_id="qq_123456", name="临雀")
        assert card.user_id == "qq_123456"
        assert card.name == "临雀"
        assert card.identity_anchors == []
        assert card.relationships == []
        assert card.short_bio == ""

    def test_roundtrip(self):
        card = UserPersonaCard(
            user_id="qq_123456",
            name="临雀",
            aliases=["狗福", "雀雀"],
            identity_anchors=["群主", "程序员"],
            relationships=[RelationshipAnchor(target_name="yuki", fact_hint="yuki是临雀朋友开发的机器人")],
            short_bio="临雀是群主，26岁程序员。",
        )
        data = card.to_dict()
        restored = UserPersonaCard.from_dict(data)
        assert restored.user_id == card.user_id
        assert restored.name == card.name
        assert restored.aliases == card.aliases
        assert restored.identity_anchors == card.identity_anchors
        assert len(restored.relationships) == 1
        assert restored.relationships[0].target_name == "yuki"


class TestRelationshipAnchor:
    def test_roundtrip(self):
        ra = RelationshipAnchor(
            target_name="yuki",
            target_user_id="qq_yuki",
            fact_hint="yuki是临雀朋友开发的QQ机器人",
            mentioned_count=3,
        )
        data = ra.to_dict()
        restored = RelationshipAnchor.from_dict(data)
        assert restored.target_name == "yuki"
        assert restored.fact_hint == "yuki是临雀朋友开发的QQ机器人"
        assert restored.mentioned_count == 3


class TestAliasEntry:
    def test_roundtrip(self):
        ae = AliasEntry(
            user_id="qq_123456",
            user_name="临雀",
            weight=3.2,
            groups=["群A"],
            mentioned_count=5,
        )
        data = ae.to_dict()
        restored = AliasEntry.from_dict(data)
        assert restored.user_id == "qq_123456"
        assert restored.weight == 3.2
        assert restored.groups == ["群A"]


# ── Store ───────────────────────────────────────────────────────


class TestBiographyStore:
    def test_save_and_load_card(self, store):
        card = UserPersonaCard(user_id="qq_111", name="Alice", identity_anchors=["群主"])
        store.save_card(card)
        loaded = store.load_card("qq_111")
        assert loaded is not None
        assert loaded.name == "Alice"
        assert loaded.identity_anchors == ["群主"]

    def test_load_nonexistent(self, store):
        assert store.load_card("no_such_user") is None

    def test_load_all_cards(self, store):
        store.save_card(UserPersonaCard(user_id="qq_111", name="Alice"))
        store.save_card(UserPersonaCard(user_id="qq_222", name="Bob"))
        cards = store.load_all_cards()
        assert len(cards) == 2

    def test_alias_index_roundtrip(self, store):
        index: dict[str, list[AliasEntry]] = {
            "狗福": [
                AliasEntry(user_id="qq_111", user_name="临雀", groups=["群A"]),
                AliasEntry(user_id="qq_222", user_name="张三", groups=["群A"]),
            ]
        }
        store.save_alias_index(index)
        loaded = store.load_alias_index()
        assert "狗福" in loaded
        assert len(loaded["狗福"]) == 2

    def test_empty_alias_index(self, store):
        assert store.load_alias_index() == {}


# ── Manager — core operations ───────────────────────────────────


class TestBiographyManagerCore:
    def test_get_card_returns_none_for_unknown(self, manager):
        assert manager.get_card("no_such") is None

    def test_feed_messages_creates_card(self, manager):
        manager.feed_messages("qq_111", "Alice", "群A", ["Alice: 你好", "Alice: 我是程序员"])
        card = manager.get_card("qq_111")
        assert card is not None
        assert card.name == "Alice"
        assert len(card.pending_messages) == 2
        assert card.pending_message_count == 2

    def test_feed_messages_truncates_long_history(self, manager):
        # Generate 100 messages — should truncate to ~2000 chars
        msgs = [f"user: {'x' * 200}" for _ in range(100)]
        manager.feed_messages("qq_111", "Test", "群A", msgs)
        card = manager.get_card("qq_111")
        total = sum(len(m) for m in card.pending_messages)
        assert total <= 2000

    @pytest.mark.asyncio
    async def test_maybe_update_not_enough_messages(self, manager):
        manager.feed_messages("qq_111", "A", "群A", ["msg1", "msg2"])
        mock_provider = AsyncMock()
        # 消息不够 8 条 → 不会触发更新，但消息保留在 pending 中
        updated = await manager.maybe_update_biography(
            "qq_111",
            persona_name="小星",
            provider_async=mock_provider,
            model_name="gpt-4o-mini",
        )
        assert updated is False
        mock_provider.generate_async.assert_not_awaited()
        # pending_messages 未被清空
        assert len(manager._ensure_card("qq_111").pending_messages) == 2

    def test_register_alias(self, manager):
        manager._register_alias("狗福", "qq_111", "临雀", "群A")
        entries = manager._alias_index.get("狗福", [])
        assert len(entries) == 1
        assert entries[0].user_id == "qq_111"

    def test_register_same_alias_different_users(self, manager):
        manager._register_alias("小明", "qq_111", "临雀", "群A")
        manager._register_alias("小明", "qq_222", "张三", "群A")
        entries = manager._alias_index.get("小明", [])
        assert len(entries) == 2

    def test_register_alias_from_profile(self, manager):
        manager.register_alias_from_profile("qq_111", "临雀", ["狗福", "雀雀"], "群A")
        assert len(manager._alias_index.get("临雀", [])) == 1
        assert len(manager._alias_index.get("狗福", [])) == 1
        assert len(manager._alias_index.get("雀雀", [])) == 1


# ── Manager — alias disambiguation ──────────────────────────────


class TestAliasDisambiguation:
    def test_single_match_high_confidence(self, manager):
        manager._register_alias("狗福", "qq_111", "临雀", "群A")
        uid, conf, alts = manager.resolve_alias("狗福", group_id="群A")
        assert uid == "qq_111"
        assert conf == 0.95
        assert alts == []

    def test_group_filtering(self, manager):
        manager._register_alias("狗福", "qq_111", "临雀", "群A")
        manager._register_alias("狗福", "qq_222", "张三", "群B")
        # In 群A, only 临雀 matches
        uid, conf, _ = manager.resolve_alias("狗福", group_id="群A")
        assert uid == "qq_111"
        # In 群C (neither), falls back to all
        uid, conf, _ = manager.resolve_alias("狗福", group_id="群C")
        assert uid is None  # can't determine, both candidates
        assert conf == 0.0

    def test_at_anchor_highest_priority(self, manager):
        manager._register_alias("狗福", "qq_111", "临雀", "群A")
        manager._register_alias("狗福", "qq_222", "张三", "群A")
        uid, conf, _ = manager.resolve_alias("狗福", group_id="群A", at_user_id="qq_222")
        assert uid == "qq_222"
        assert conf == 0.98

    def test_recent_speaker_priority(self, manager):
        manager._register_alias("狗福", "qq_111", "临雀", "群A")
        manager._register_alias("狗福", "qq_222", "张三", "群A")
        uid, conf, _ = manager.resolve_alias(
            "狗福", group_id="群A", recent_speakers=["qq_111", "qq_333"]
        )
        assert uid == "qq_111"
        assert conf == 0.75

    def test_weight_gap_priority(self, manager):
        manager._register_alias("狗福", "qq_111", "临雀", "群A")
        manager._register_alias("狗福", "qq_222", "张三", "群A")
        # Boost 临雀's weight
        manager.bump_alias_weight("狗福", "qq_111", "群A")
        manager.bump_alias_weight("狗福", "qq_111", "群A")
        # 临雀 weight = 1.0 + 0.3 + 0.3 = 1.6, 张三 weight = 1.0 * 0.98 * 0.98 = 0.9604
        uid, conf, _ = manager.resolve_alias("狗福", group_id="群A")
        assert uid == "qq_111"
        assert conf == 0.6

    def test_bump_weight_decays_others(self, manager):
        manager._register_alias("狗福", "qq_111", "临雀", "群A")
        manager._register_alias("狗福", "qq_222", "张三", "群A")
        old_w2 = manager._alias_index["狗福"][1].weight
        manager.bump_alias_weight("狗福", "qq_111", "群A")
        assert manager._alias_index["狗福"][0].weight > 1.0
        assert manager._alias_index["狗福"][1].weight < old_w2

    def test_unknown_alias_returns_none(self, manager):
        uid, conf, alts = manager.resolve_alias("nobody")
        assert uid is None
        assert conf == 0.0
        assert alts == []

    def test_get_aliases_for_group(self, manager):
        manager._register_alias("狗福", "qq_111", "临雀", "群A")
        manager._register_alias("雀雀", "qq_111", "临雀", "群A")
        manager._register_alias("狗福", "qq_222", "张三", "群B")
        aliases = manager.get_aliases_for_group("群A")
        assert aliases["狗福"] == "临雀"
        assert aliases["雀雀"] == "临雀"


# ── Manager — LLM update (async mock) ───────────────────────────


class TestBiographyUpdate:
    @pytest.mark.asyncio
    async def test_maybe_update_triggers_on_enough_messages(self, manager):
        msgs = [f"临雀: msg{i}" for i in range(10)]
        manager.feed_messages("qq_111", "临雀", "群A", msgs)

        mock_provider = AsyncMock()
        bio_response = (
            '{"short_bio": "临雀是群主，26岁程序员。", '
            '"identity_anchors": ["群主", "程序员"], '
            '"relationships": [{"target": "yuki", "fact_hint": "yuki是临雀朋友开发的机器人"}], '
            '"discovered_aliases": ["雀雀"]}'
        )
        mock_provider.generate_async.return_value = bio_response

        updated = await manager.maybe_update_biography(
            "qq_111",
            persona_name="小星",
            provider_async=mock_provider,
            model_name="gpt-4o-mini",
        )
        assert updated is True
        card = manager.get_card("qq_111")
        assert card is not None
        assert "临雀是群主" in card.short_bio
        assert "群主" in card.identity_anchors
        assert len(card.relationships) == 1
        assert card.pending_messages == []

    @pytest.mark.asyncio
    async def test_maybe_update_skips_with_few_messages(self, manager):
        manager.feed_messages("qq_111", "A", "群A", ["msg1", "msg2"])
        mock_provider = AsyncMock()
        updated = await manager.maybe_update_biography(
            "qq_111",
            persona_name="小星",
            provider_async=mock_provider,
            model_name="gpt-4o-mini",
        )
        assert updated is False
        mock_provider.generate_async.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_maybe_update_no_pending_messages(self, manager):
        mock_provider = AsyncMock()
        updated = await manager.maybe_update_biography(
            "qq_111",
            persona_name="小星",
            provider_async=mock_provider,
            model_name="gpt-4o-mini",
        )
        assert updated is False

    @pytest.mark.asyncio
    async def test_maybe_update_handles_llm_failure(self, manager):
        msgs = [f"临雀: msg{i}" for i in range(10)]
        manager.feed_messages("qq_111", "临雀", "群A", msgs)
        mock_provider = AsyncMock()
        mock_provider.generate_async.side_effect = RuntimeError("timeout")
        updated = await manager.maybe_update_biography(
            "qq_111",
            persona_name="小星",
            provider_async=mock_provider,
            model_name="gpt-4o-mini",
        )
        assert updated is False

    @pytest.mark.asyncio
    async def test_maybe_update_handles_invalid_json(self, manager):
        msgs = [f"临雀: msg{i}" for i in range(10)]
        manager.feed_messages("qq_111", "临雀", "群A", msgs)
        mock_provider = AsyncMock()
        mock_provider.generate_async.return_value = "这不是 JSON"
        updated = await manager.maybe_update_biography(
            "qq_111",
            persona_name="小星",
            provider_async=mock_provider,
            model_name="gpt-4o-mini",
        )
        assert updated is False


# ── Prompt builder ──────────────────────────────────────────────


class TestBuildUpdatePrompt:
    def test_empty_card(self):
        prompt = _build_update_prompt(
            user_name="临雀",
            persona_name="小星",
            old_bio="",
            old_anchors=[],
            old_relationships=[],
            messages=["临雀: 我最近在学Rust"],
        )
        assert "临雀" in prompt
        assert "尚无传记" in prompt
        assert "我最近在学Rust" in prompt

    def test_with_existing_bio(self):
        prompt = _build_update_prompt(
            user_name="临雀",
            persona_name="小星",
            old_bio="临雀是群主，26岁程序员。",
            old_anchors=["群主", "程序员"],
            old_relationships=[RelationshipAnchor(target_name="yuki", fact_hint="yuki是临雀朋友开发的")],
            messages=["临雀: yuki今天改进了天气预报功能"],
        )
        assert "临雀是群主" in prompt
        assert "群主" in prompt
        assert "yuki" in prompt
        assert "天气预报" in prompt
