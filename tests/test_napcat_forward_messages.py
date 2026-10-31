"""入站合并转发应被递归读取，节点里的图片要真正进入视觉通道。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from sirius_pulse.platforms.onebot_v11.napcat.adapter import NapCatAdapter


class _FakeBrain:
    """记录 raw_call 请求的假 Brain，用来验证压缩只调用一次。"""

    def __init__(self, result: str = "", error: Exception | None = None) -> None:
        self.requests: list = []
        self.result = result
        self.error = error

    async def raw_call(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.result


def _group_event(message: list[dict]) -> dict:
    return {
        "time": 1720000000,
        "post_type": "message",
        "message_type": "group",
        "group_id": "100",
        "user_id": "300",
        "self_id": "100",
        "message_id": "message-1",
        "message": message,
        "sender": {"nickname": "Alice", "card": ""},
    }


def _adapter(
    monkeypatch: pytest.MonkeyPatch,
    responses: dict[str, dict | Exception],
    *,
    work_path: Path,
) -> NapCatAdapter:
    adapter = NapCatAdapter(
        "ws://example.invalid",
        work_path=work_path,
        config={"allowed_group_ids": ["100"], "persona_name": "alpha", "qq_number": "100"},
    )
    calls: list[tuple[str, dict]] = []

    async def fake_call_api(action: str, params: dict) -> dict:
        calls.append((action, params))
        assert action == "get_forward_msg"
        result = responses[params["message_id"]]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(adapter, "call_api", fake_call_api)
    adapter.forward_calls = calls  # type: ignore[attr-defined]
    return adapter


@pytest.mark.asyncio
async def test_forward_message_reads_nested_nodes_and_images(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """合并转发里的文字、嵌套转发和图片都应进入模型可见的输入。"""
    outer_image = tmp_path / "outer.png"
    outer_image.write_bytes(b"outer-image")
    inner_image = tmp_path / "inner.png"
    inner_image.write_bytes(b"inner-image")
    adapter = _adapter(
        monkeypatch,
        {
            "fwd-1": {
                "status": "ok",
                "data": {
                    "messages": [
                        {
                            "type": "node",
                            "data": {
                                "nickname": "张三",
                                "content": [
                                    {"type": "text", "data": {"text": "看看这个"}},
                                    {"type": "image", "data": {"file": str(outer_image)}},
                                ],
                            },
                        },
                        {
                            "type": "node",
                            "data": {
                                "nickname": "李四",
                                "content": [{"type": "forward", "data": {"id": "fwd-2"}}],
                            },
                        },
                    ]
                },
            },
            "fwd-2": {
                "status": "ok",
                "data": {
                    "messages": [
                        {
                            "nickname": "王五",
                            "message": [
                                {"type": "text", "data": {"text": "内层消息"}},
                                {"type": "image", "data": {"file": str(inner_image)}},
                            ],
                        }
                    ]
                },
            },
        },
        work_path=tmp_path,
    )

    parsed = await adapter.parse_event(_group_event([{"type": "forward", "data": {"id": "fwd-1"}}]))

    assert parsed is not None
    assert "[合并转发：共2条]" in parsed.prompt
    assert "张三：看看这个" in parsed.prompt
    assert "李四：" in parsed.prompt
    assert "[合并转发：共1条]" in parsed.prompt
    assert "王五：内层消息" in parsed.prompt
    assert parsed.prompt.count("图片") == 2
    assert [call[1]["message_id"] for call in adapter.forward_calls] == ["fwd-1", "fwd-2"]

    assert len(parsed.multimodal_inputs) == 2
    cached = [Path(item["value"]) for item in parsed.multimodal_inputs]
    assert all(path.parent == adapter._image_cache_dir for path in cached)
    assert [path.read_bytes() for path in cached] == [b"outer-image", b"inner-image"]


@pytest.mark.asyncio
async def test_forward_message_stops_at_depth_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """嵌套超过上限时保留占位说明，不再继续请求下一层。"""

    def _nested(forward_id: str, text: str) -> dict:
        return {
            "status": "ok",
            "data": {
                "messages": [
                    {
                        "type": "node",
                        "data": {
                            "nickname": "甲",
                            "content": [
                                {"type": "text", "data": {"text": text}},
                                {"type": "forward", "data": {"id": forward_id}},
                            ],
                        },
                    }
                ]
            },
        }

    adapter = _adapter(
        monkeypatch,
        {
            "fwd-1": _nested("fwd-2", "第一层"),
            "fwd-2": _nested("fwd-3", "第二层"),
            "fwd-3": _nested("fwd-4", "第三层"),
            "fwd-4": _nested("fwd-5", "第四层"),
        },
        work_path=tmp_path,
    )

    parsed = await adapter.parse_event(_group_event([{"type": "forward", "data": {"id": "fwd-1"}}]))

    assert parsed is not None
    for text in ("第一层", "第二层", "第三层", "第四层"):
        assert text in parsed.prompt
    assert "层级过深未展开" in parsed.prompt
    assert "fwd-5" not in [call[1]["message_id"] for call in adapter.forward_calls]


@pytest.mark.asyncio
async def test_forward_fetch_failure_keeps_message_visible(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """读不到转发内容时仍要让模型知道有人转发了一条记录，不能整条消息消失。"""
    adapter = _adapter(
        monkeypatch,
        {"fwd-broken": RuntimeError("API timeout: get_forward_msg")},
        work_path=tmp_path,
    )

    parsed = await adapter.parse_event(
        _group_event(
            [
                {"type": "text", "data": {"text": "你看这个"}},
                {"type": "forward", "data": {"id": "fwd-broken"}},
            ]
        )
    )

    assert parsed is not None
    assert "你看这个" in parsed.prompt
    assert "内容读取失败" in parsed.prompt
    assert parsed.multimodal_inputs == []


@pytest.mark.asyncio
async def test_forward_message_is_truncated_to_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """超长合并转发只展开预算内的节点，并说明还有剩余。"""
    nodes = [
        {
            "type": "node",
            "data": {
                "nickname": f"用户{index}",
                "content": [{"type": "text", "data": {"text": f"第{index}条内容"}}],
            },
        }
        for index in range(1, 46)
    ]
    adapter = _adapter(
        monkeypatch,
        {"fwd-long": {"status": "ok", "data": {"messages": nodes}}},
        work_path=tmp_path,
    )

    parsed = await adapter.parse_event(
        _group_event([{"type": "forward", "data": {"id": "fwd-long"}}])
    )

    assert parsed is not None
    assert "未展开" in parsed.prompt
    assert "第40条内容" in parsed.prompt
    assert "第41条内容" not in parsed.prompt
    assert len(parsed.prompt) < 4000
    assert "[未展开内容概要]" not in parsed.prompt


@pytest.mark.asyncio
async def test_overflow_is_compressed_by_one_llm_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """超出预算的转发正文应额外压成概要，且只调用一次 LLM。"""
    nodes = [
        {
            "type": "node",
            "data": {
                "nickname": f"用户{index}",
                "content": [{"type": "text", "data": {"text": f"第{index}条内容"}}],
            },
        }
        for index in range(1, 46)
    ]
    adapter = _adapter(
        monkeypatch,
        {"fwd-long": {"status": "ok", "data": {"messages": nodes}}},
        work_path=tmp_path,
    )
    brain = _FakeBrain("第41到45条：大家在约周末聚餐，地点定在南门。")
    adapter._engine = SimpleNamespace(brain=brain)  # type: ignore[assignment]

    parsed = await adapter.parse_event(
        _group_event([{"type": "forward", "data": {"id": "fwd-long"}}])
    )

    assert parsed is not None
    assert "[未展开内容概要] 第41到45条" in parsed.prompt
    assert parsed.prompt.index("[未展开内容概要]") < parsed.prompt.index("[/合并转发]")
    assert "第41条内容" not in parsed.prompt
    assert len(brain.requests) == 1
    request = brain.requests[0]
    assert request.model == "work_mode_generate"
    assert "第41条内容" in request.messages[0]["content"]
    assert "第1条内容" not in request.messages[0]["content"]


@pytest.mark.asyncio
async def test_forward_summary_failure_keeps_truncation_note(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """压缩失败不能影响消息本身，原有省略说明仍然保留。"""
    nodes = [
        {
            "type": "node",
            "data": {
                "nickname": f"用户{index}",
                "content": [{"type": "text", "data": {"text": f"第{index}条内容"}}],
            },
        }
        for index in range(1, 46)
    ]
    adapter = _adapter(
        monkeypatch,
        {"fwd-long": {"status": "ok", "data": {"messages": nodes}}},
        work_path=tmp_path,
    )
    brain = _FakeBrain(error=RuntimeError("AMKR 404: work_mode_generate"))
    adapter._engine = SimpleNamespace(brain=brain)  # type: ignore[assignment]

    parsed = await adapter.parse_event(
        _group_event([{"type": "forward", "data": {"id": "fwd-long"}}])
    )

    assert parsed is not None
    assert "第40条内容" in parsed.prompt
    assert "后续 5 条未展开" in parsed.prompt
    assert "[未展开内容概要]" not in parsed.prompt
    assert len(brain.requests) == 1


@pytest.mark.asyncio
async def test_clipped_node_tail_is_sent_to_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """单条节点被截断时，截掉的尾部也要进入压缩输入，而不是直接丢弃。"""
    tail = "尾部关键结论" * 100
    node = {
        "type": "node",
        "data": {
            "nickname": "张三",
            "content": [{"type": "text", "data": {"text": "开头" * 400 + tail}}],
        },
    }
    adapter = _adapter(
        monkeypatch,
        {"fwd-clip": {"status": "ok", "data": {"messages": [node]}}},
        work_path=tmp_path,
    )
    brain = _FakeBrain("张三说了一段很长的内容，结论在最后。")
    adapter._engine = SimpleNamespace(brain=brain)  # type: ignore[assignment]

    parsed = await adapter.parse_event(
        _group_event([{"type": "forward", "data": {"id": "fwd-clip"}}])
    )

    assert parsed is not None
    assert "[未展开内容概要]" in parsed.prompt
    assert "尾部关键结论" not in parsed.prompt
    assert "尾部关键结论" in brain.requests[0].messages[0]["content"]


@pytest.mark.asyncio
async def test_short_forward_does_not_call_summary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """预算内的转发不应产生额外的 LLM 调用。"""
    adapter = _adapter(
        monkeypatch,
        {
            "fwd-short": {
                "status": "ok",
                "data": {
                    "messages": [
                        {
                            "type": "node",
                            "data": {
                                "nickname": "张三",
                                "content": [{"type": "text", "data": {"text": "一句话"}}],
                            },
                        }
                    ]
                },
            }
        },
        work_path=tmp_path,
    )
    brain = _FakeBrain("不应该被调用")
    adapter._engine = SimpleNamespace(brain=brain)  # type: ignore[assignment]

    parsed = await adapter.parse_event(
        _group_event([{"type": "forward", "data": {"id": "fwd-short"}}])
    )

    assert parsed is not None
    assert "一句话" in parsed.prompt
    assert brain.requests == []


@pytest.mark.asyncio
async def test_quote_of_forward_message_expands_its_content(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """回复一条合并转发时，引用里也应带上转发正文和图片。"""
    quoted_image = tmp_path / "quoted.png"
    quoted_image.write_bytes(b"quoted-image")
    adapter = NapCatAdapter(
        "ws://example.invalid",
        work_path=tmp_path,
        config={"allowed_group_ids": ["100"], "persona_name": "alpha", "qq_number": "100"},
    )

    async def fake_call_api(action: str, params: dict) -> dict:
        if action == "get_msg":
            return {
                "status": "ok",
                "data": {
                    "message": [{"type": "forward", "data": {"id": "fwd-quoted"}}],
                    "sender": {"nickname": "Bob"},
                },
            }
        assert action == "get_forward_msg" and params["message_id"] == "fwd-quoted"
        return {
            "status": "ok",
            "data": {
                "messages": [
                    {
                        "type": "node",
                        "data": {
                            "nickname": "张三",
                            "content": [
                                {"type": "text", "data": {"text": "被转发的正文"}},
                                {"type": "image", "data": {"file": str(quoted_image)}},
                            ],
                        },
                    }
                ]
            },
        }

    monkeypatch.setattr(adapter, "call_api", fake_call_api)

    parsed = await adapter.parse_event(
        _group_event(
            [
                {"type": "reply", "data": {"id": "555"}},
                {"type": "text", "data": {"text": "这是什么"}},
            ]
        )
    )

    assert parsed is not None
    assert 'msg_id="555"' in parsed.prompt
    assert "被转发的正文" in parsed.prompt
    assert "[合并转发：共1条]" in parsed.prompt
    assert len(parsed.multimodal_inputs) == 1
    cached = Path(parsed.multimodal_inputs[0]["value"])
    assert cached.parent == adapter._image_cache_dir
    assert cached.read_bytes() == b"quoted-image"


@pytest.mark.asyncio
async def test_private_forward_message_is_rendered(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """私聊里的合并转发同样要展开。"""
    adapter = _adapter(
        monkeypatch,
        {
            "fwd-private": {
                "status": "ok",
                "data": {
                    "messages": [
                        {
                            "type": "node",
                            "data": {
                                "nickname": "张三",
                                "content": [{"type": "text", "data": {"text": "私聊转发内容"}}],
                            },
                        }
                    ]
                },
            }
        },
        work_path=tmp_path,
    )
    event = {
        "time": 1720000000,
        "post_type": "message",
        "message_type": "private",
        "user_id": "300",
        "self_id": "100",
        "message_id": "message-2",
        "message": [{"type": "forward", "data": {"id": "fwd-private"}}],
        "sender": {"nickname": "Alice", "card": ""},
    }

    parsed = await adapter.parse_event(event)

    assert parsed is not None
    assert parsed.group_id == "private_300"
    assert "张三：私聊转发内容" in parsed.prompt


@pytest.mark.asyncio
async def test_self_referencing_forward_is_marked_as_cycle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """转发引用自己时只读一次，并明确标注循环引用。"""
    adapter = _adapter(
        monkeypatch,
        {
            "fwd-loop": {
                "status": "ok",
                "data": {
                    "messages": [
                        {
                            "type": "node",
                            "data": {
                                "nickname": "甲",
                                "content": [
                                    {"type": "text", "data": {"text": "我是自己"}},
                                    {"type": "forward", "data": {"id": "fwd-loop"}},
                                ],
                            },
                        }
                    ]
                },
            }
        },
        work_path=tmp_path,
    )

    parsed = await adapter.parse_event(
        _group_event([{"type": "forward", "data": {"id": "fwd-loop"}}])
    )

    assert parsed is not None
    assert "循环引用" in parsed.prompt
    assert len(adapter.forward_calls) == 1


@pytest.mark.asyncio
async def test_forward_fetch_budget_caps_broken_nested_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """大量失效的嵌套转发不能放大成几十次串行 API 调用。"""
    nodes = [
        {
            "type": "node",
            "data": {
                "nickname": f"用户{index}",
                "content": [{"type": "forward", "data": {"id": f"broken-{index}"}}],
            },
        }
        for index in range(1, 41)
    ]
    adapter = _adapter(
        monkeypatch,
        {},
        work_path=tmp_path,
    )

    async def fake_call_api(action: str, params: dict) -> dict:
        adapter.forward_calls.append((action, params))
        raise RuntimeError("API error: get_forward_msg retcode=100")

    monkeypatch.setattr(adapter, "call_api", fake_call_api)

    parsed = await adapter.parse_event(
        _group_event(
            [
                {
                    "type": "forward",
                    "data": {"content": nodes},
                }
            ]
        )
    )

    assert parsed is not None
    assert "内容读取失败" in parsed.prompt
    assert len(adapter.forward_calls) <= 24


@pytest.mark.asyncio
async def test_forward_and_top_level_images_keep_prompt_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """顶层图片在合并转发之前时，视觉输入顺序必须与正文标签一致。"""
    top_image = tmp_path / "top.png"
    top_image.write_bytes(b"top-image")
    forward_image = tmp_path / "forward.png"
    forward_image.write_bytes(b"forward-image")
    adapter = _adapter(
        monkeypatch,
        {
            "fwd-order": {
                "status": "ok",
                "data": {
                    "messages": [
                        {
                            "type": "node",
                            "data": {
                                "nickname": "张三",
                                "content": [
                                    {"type": "text", "data": {"text": "转发里的图"}},
                                    {"type": "image", "data": {"file": str(forward_image)}},
                                ],
                            },
                        }
                    ]
                },
            }
        },
        work_path=tmp_path,
    )

    parsed = await adapter.parse_event(
        _group_event(
            [
                {"type": "image", "data": {"file": str(top_image)}},
                {"type": "forward", "data": {"id": "fwd-order"}},
            ]
        )
    )

    assert parsed is not None
    assert parsed.prompt.index("top.png") < parsed.prompt.index("forward.png")
    assert [Path(item["value"]).read_bytes() for item in parsed.multimodal_inputs] == [
        b"top-image",
        b"forward-image",
    ]


@pytest.mark.asyncio
async def test_inline_forward_segment_content_is_rendered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """forward 段直接带消息段数组时，应当作一个节点的正文，而不是节点列表。"""
    adapter = _adapter(monkeypatch, {}, work_path=tmp_path)

    parsed = await adapter.parse_event(
        _group_event(
            [
                {
                    "type": "forward",
                    "data": {
                        "content": [
                            {"type": "text", "data": {"text": "内联正文"}},
                            {"type": "text", "data": {"text": "第二段"}},
                        ]
                    },
                }
            ]
        )
    )

    assert parsed is not None
    assert "[合并转发：共1条]" in parsed.prompt
    assert "内联正文" in parsed.prompt
    assert "第二段" in parsed.prompt
    assert adapter.forward_calls == []
