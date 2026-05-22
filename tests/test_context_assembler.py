"""Tests for context assembler."""

from __future__ import annotations

from sirius_pulse.memory.basic import BasicMemoryManager
from sirius_pulse.memory.diary import DiaryIndexer, DiaryRetriever, DiaryEntry
from sirius_pulse.memory.context_assembler import ContextAssembler


class TestContextAssembler:
    def test_build_messages_with_diary(self) -> None:
        basic = BasicMemoryManager()
        basic.add_entry("g1", "alice", "human", "你好")
        basic.add_entry("g1", "assistant", "assistant", "你好呀")

        indexer = DiaryIndexer(enable_semantic=False)
        indexer.add(DiaryEntry("d1", "g1", "2026-04-22T10:00:00+00:00", content="之前聊过问候", summary="问候日记"))
        retriever = DiaryRetriever(indexer)

        assembler = ContextAssembler(basic, retriever)
        messages = assembler.build_messages(
            "g1", "问候", "你是助手",
            recent_n=5,
            diary_top_k=5,
            diary_token_budget=800,
        )

        # Should return exactly 2 messages: system (with XML history + diary) + user (current)
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        # 第 1 条属于前 5 档，注入完整 content，不显示 summary
        assert "之前聊过问候" in messages[0]["content"]
        assert "问候日记" not in messages[0]["content"]
        # History is embedded in system prompt as XML
        assert "<conversation_history>" in messages[0]["content"]
        assert 'speaker="alice"' in messages[0]["content"]
        assert 'speaker="assistant"' in messages[0]["content"]
        assert messages[1]["role"] == "user"
        assert messages[1]["content"] == "问候"

    def test_build_messages_without_diary(self) -> None:
        basic = BasicMemoryManager()
        basic.add_entry("g1", "alice", "human", "hi")

        indexer = DiaryIndexer(enable_semantic=False)
        retriever = DiaryRetriever(indexer)

        assembler = ContextAssembler(basic, retriever)
        messages = assembler.build_messages("g1", "hello", "sys")

        # Should return exactly 2 messages: system (with XML history) + user (current)
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert "<conversation_history>" in messages[0]["content"]
        assert messages[1]["role"] == "user"
        assert messages[1]["content"] == "hello"

    def test_build_history_xml(self) -> None:
        basic = BasicMemoryManager()
        basic.add_entry("g1", "alice", "human", "你好", speaker_name="Alice")
        basic.add_entry("g1", "assistant", "assistant", "你好呀", speaker_name="小星")

        indexer = DiaryIndexer(enable_semantic=False)
        retriever = DiaryRetriever(indexer)

        assembler = ContextAssembler(basic, retriever)
        xml = assembler.build_history_xml("g1", n=5)

        assert "<conversation_history>" in xml
        assert "</conversation_history>" in xml
        assert 'speaker="Alice"' in xml
        assert 'speaker="小星"' in xml
        assert 'role="user"' in xml
        assert 'role="assistant"' in xml
        assert "你好" in xml
        assert "你好呀" in xml

    def test_xml_escaping(self) -> None:
        basic = BasicMemoryManager()
        basic.add_entry("g1", "alice", "human", "<script>alert('xss')</script>", speaker_name="Alice")

        indexer = DiaryIndexer(enable_semantic=False)
        retriever = DiaryRetriever(indexer)

        assembler = ContextAssembler(basic, retriever)
        xml = assembler.build_history_xml("g1", n=5)

        # Content should be escaped, raw HTML tags should not appear
        assert "<script>" not in xml
        assert "&lt;script&gt;" in xml

    def test_diary_injection_tiers(self) -> None:
        """分级注入：最多 12 条，前 5 条注入全文，其余仅摘要。"""
        basic = BasicMemoryManager()
        basic.add_entry("g1", "alice", "human", "你好")

        indexer = DiaryIndexer(enable_semantic=False)
        for i in range(1, 16):
            indexer.add(
                DiaryEntry(
                    f"d{i}",
                    "g1",
                    f"2026-04-22T10:0{i}:00+00:00",
                    content=f"query日记正文{i}",
                    summary=f"摘要{i}",
                )
            )
        retriever = DiaryRetriever(indexer)

        assembler = ContextAssembler(basic, retriever)
        messages = assembler.build_messages("g1", "query", "sys")
        lines = messages[0]["content"].splitlines()

        # 只注入前 12 条
        for i in range(1, 13):
            assert any(line.startswith(f"{i}.") for line in lines)
        for i in range(13, 16):
            assert not any(f"摘要{i}" in line for line in lines)

        # 前 5 条注入完整 content，不显示 summary，且带时间戳
        for i in range(1, 6):
            assert f"query日记正文{i}" in "\n".join(lines)
            assert f"[2026-04-22 10:0{i}]" in "\n".join(lines)
            assert f"摘要{i}" not in lines

        # 第 6 条及以后仅注入摘要，不含正文
        for i in range(6, 13):
            assert f"摘要{i}" in "\n".join(lines)
            assert f"query日记正文{i}" not in lines
