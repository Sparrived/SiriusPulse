"""表情包「只留描述、原图用完即删」的持久化回归（用户视角）。

线上那个人格目录攒到 639MB / 1524 张表情包的循环：表情包被下载进
``{work_path}/sticker_cache/``，读图拿到描述，原图却一直留在磁盘上；而描述只活在
内存里，进程一重启，同一张表情包再到达还得重新读一遍图。

这里只看三件用户可观察的事：

1. 模型读到的会话历史里有没有真实描述，而不是默认占位「动画表情」；
2. 磁盘上还剩什么（``sticker_cache`` / ``image_cache`` / ``sticker_captions.json``）；
3. 读图模型被调用了几次（``MockProvider`` 计数）。

进程重启用同一个 ``work_path`` 重新构造引擎来模拟：``work_path`` 就是
``data/personas/<人格>``，进程换了、盘上的文件没换。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from sirius_pulse.config.models import ExpressivenessConfig
from sirius_pulse.core.cognition import CognitionAnalyzer
from sirius_pulse.core.delayed_response_queue import DelayedResponseQueue
from sirius_pulse.core.engine_core import _EmotionalGroupChatEngineBase
from sirius_pulse.core.pipeline import Pipeline
from sirius_pulse.core.rhythm import RhythmAnalyzer
from sirius_pulse.memory.basic.manager import BasicMemoryManager
from sirius_pulse.memory.context_assembler import ContextAssembler
from sirius_pulse.models.models import Message
from sirius_pulse.providers.mock import MockProvider

_GROUP_ID = "123456"
_STICKER_CAPTION = "一只橘猫瘫在键盘上，表情生无可恋。"
_PHOTO_CAPTION = "屏幕上是一张报错截图，红字提示连接超时。"
_DEFAULT_STICKER_CAPTION = "动画表情"
_STICKER_BYTES = b"fake-sticker-bytes-orange-cat"


class _RaisingVisionProvider(MockProvider):
    """读图模型直接报错：引擎必须把它当成「这次没读出内容」，而不是崩掉。"""

    async def generate_async(self, request: Any, return_reasoning: bool = False) -> Any:
        self.requests.append(request)
        raise RuntimeError("读图模型炸了")


def _participants() -> list[SimpleNamespace]:
    return [SimpleNamespace(user_id="u1", name="Alice", is_developer=False, identities={})]


def _new_engine(
    work_path: Path,
    provider: MockProvider | None = None,
) -> tuple[_EmotionalGroupChatEngineBase, MockProvider]:
    """构造一个会真正写基础记忆、并且知道人格目录的引擎。

    参考 ``tests/test_sticker_caption_context.py::_engine_for_sticker_caption``，
    区别是这里必须给引擎 ``work_path``：原图即删与描述落盘都以人格目录为根。
    """
    engine = object.__new__(_EmotionalGroupChatEngineBase)
    provider = provider if provider is not None else MockProvider([])

    engine.persona = SimpleNamespace(name="Luna", aliases=["月白"], reply_frequency="moderate")
    engine.config = {}
    engine.work_path = work_path
    engine._current_adapter_type = ""
    engine.expressiveness = ExpressivenessConfig()
    engine.cognition_analyzer = CognitionAnalyzer(
        provider_async=provider,
        ai_name="Luna",
        ai_aliases=["月白"],
        work_path=work_path,
    )
    engine.cognition_store = SimpleNamespace(add=lambda **kwargs: None)
    engine.rhythm_analyzer = RhythmAnalyzer()
    engine._helpers = SimpleNamespace(get_recent_messages=lambda group_id, n: [])
    engine._last_reply_at = {}
    engine._topic_window = {}
    engine._topic_window_max_size = 10
    engine._delayed_event_emitted = {}
    engine.basic_memory = BasicMemoryManager()
    engine.basic_store = SimpleNamespace(append=lambda entry: None)
    engine.event_bus = SimpleNamespace(emit=AsyncMock(return_value=True))
    engine.delayed_queue = DelayedResponseQueue()
    engine.assistant_emotion = SimpleNamespace(
        update_from_interaction=lambda emotion, user_id: None
    )
    engine.semantic_memory = SimpleNamespace(
        get_user_profile=lambda group_id, user_id: None,
        settle_engagement=lambda **kwargs: None,
        record_interaction=lambda **kwargs: None,
        set_user_profile_fields=lambda *args, **kwargs: None,
    )
    engine._persistence = SimpleNamespace(persist_group_state=lambda group_id: None)
    engine._log_inner_thought = lambda *args, **kwargs: None
    engine._work_mode_runs = {}
    engine._group_last_message_at = {}
    engine.identity_resolver = SimpleNamespace(
        resolve=lambda *args, **kwargs: None,
        resolve_with_alias=lambda *args, **kwargs: SimpleNamespace(
            user_id="u1", confidence=1.0, source="alias"
        ),
    )
    engine.user_manager = SimpleNamespace(
        get_user=lambda user_id, group_id: SimpleNamespace(
            user_id=user_id,
            name="Alice",
            is_developer=False,
            identities={},
            identity_anchors=[],
        )
    )

    pipeline = Pipeline(engine)
    pipeline.background_update = lambda group_id, message, emotion, intent, user_id: None
    engine._pipeline = pipeline

    return engine, provider


def _arrive_sticker(
    work_path: Path,
    *,
    name: str = "b1f2c3d4e5f6a7b8.gif",
    data: bytes = _STICKER_BYTES,
    with_url_sidecar: bool = False,
) -> dict[str, str]:
    """NapCat 把一个表情包落到人格缓存目录后的多模态输入项。"""
    cache_dir = work_path / "sticker_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / name
    path.write_bytes(data)
    if with_url_sidecar:
        Path(f"{path}.url").write_text("https://example.test/sticker.gif", encoding="utf-8")
    return {"type": "image", "value": str(path), "file_path": str(path), "sub_type": "1"}


def _file_url_sticker(path: Path) -> dict[str, str]:
    """上游有时给的是 ``file://`` 引用（Windows 上形如 ``file:///C:/...``）。"""
    return {"type": "image", "value": path.as_uri(), "file_path": str(path), "sub_type": "1"}


def _plain_path_sticker(path: Path) -> dict[str, str]:
    """最常见的形态：NapCat 落盘后的本地绝对路径。"""
    return {"type": "image", "value": str(path), "file_path": str(path), "sub_type": "1"}


def _arrive_photo(
    work_path: Path,
    *,
    name: str = "a1b2c3d4e5f6.png",
    data: bytes = b"fake-photo-bytes",
) -> dict[str, str]:
    """普通图片落进 image_cache（没有 sub_type，不是表情包）。"""
    cache_dir = work_path / "image_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / name
    path.write_bytes(data)
    Path(f"{path}.url").write_text("https://example.test/photo.png", encoding="utf-8")
    return {"type": "image", "value": str(path), "file_path": str(path)}


def _image_label(image: dict[str, str]) -> str:
    """适配器渲染出的纯图片内容标签：``[动画表情："xxx.gif"]`` / ``[图片："a.png"]``。"""
    label = "动画表情" if image.get("sub_type") == "1" else "图片"
    return f'[{label}："{Path(image["value"]).name}"]'


def _incoming(image: dict[str, str], *, message_id: str) -> Message:
    return Message(
        role="user",
        content=_image_label(image),
        speaker="Alice",
        channel="group",
        channel_user_id="200",
        message_id=message_id,
        multimodal_inputs=[image],
    )


def _model_visible_history(engine: _EmotionalGroupChatEngineBase, group_id: str = _GROUP_ID) -> str:
    """模型真正读到的会话历史 XML（与拼 prompt 时同一个渲染函数）。"""
    return ContextAssembler._entries_to_xml(engine.basic_memory.get_all(group_id))


def _assert_model_sees_caption(xml: str, caption: str) -> None:
    assert f'<image type="sticker" caption="{caption}"' in xml, xml
    # 默认占位符一旦出现在历史里，模型还是只会知道「这是个动画表情」。
    assert f'caption="{_DEFAULT_STICKER_CAPTION}"' not in xml, xml


def _persisted_captions(work_path: Path) -> dict[str, str]:
    return json.loads((work_path / "sticker_captions.json").read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_sticker_leaves_only_its_caption_on_disk(tmp_path):
    """群里发一张表情包：历史里有真实描述，缓存目录里什么都不剩。"""
    engine, provider = _new_engine(tmp_path, MockProvider([_STICKER_CAPTION]))
    sticker = _arrive_sticker(tmp_path, with_url_sidecar=True)
    sticker_path = Path(sticker["value"])

    await engine.process_message(_incoming(sticker, message_id="msg-1"), _participants(), _GROUP_ID)

    _assert_model_sees_caption(_model_visible_history(engine), _STICKER_CAPTION)
    assert len(provider.requests) == 1

    # 原图与遗留的 .url 侧车都被清掉，sticker_cache 不再堆积。
    assert not sticker_path.exists()
    assert not Path(f"{sticker_path}.url").exists()
    assert list((tmp_path / "sticker_cache").iterdir()) == []

    # 描述以「图片内容 sha256 → 描述」落盘，重启后还能命中。
    assert _persisted_captions(tmp_path) == {
        hashlib.sha256(_STICKER_BYTES).hexdigest(): _STICKER_CAPTION
    }


@pytest.mark.asyncio
async def test_same_sticker_again_costs_no_second_vision_call(tmp_path):
    """同一张表情包再发一次：不再读图，历史里两次都能看懂。"""
    engine, provider = _new_engine(tmp_path, MockProvider([_STICKER_CAPTION]))

    for index in (1, 2):
        # NapCat 会为同一条表情包重新落一份同样的字节。
        sticker = _arrive_sticker(tmp_path)
        await engine.process_message(
            _incoming(sticker, message_id=f"msg-{index}"), _participants(), _GROUP_ID
        )

    xml = _model_visible_history(engine)
    assert xml.count('<image type="sticker"') == 2
    assert xml.count(f'caption="{_STICKER_CAPTION}"') == 2
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_restarted_persona_reuses_persisted_caption(tmp_path):
    """人格进程重启后同一张表情包再到达：一次读图都不该发生，描述仍可用。"""
    first_engine, first_provider = _new_engine(tmp_path, MockProvider([_STICKER_CAPTION]))
    await first_engine.process_message(
        _incoming(_arrive_sticker(tmp_path), message_id="msg-before-restart"),
        _participants(),
        _GROUP_ID,
    )
    assert len(first_provider.requests) == 1
    assert (tmp_path / "sticker_captions.json").is_file(), "前置条件：描述已经落盘"

    # 模拟进程重启：同一个 work_path，新引擎、全新的内存缓存。
    second_engine, second_provider = _new_engine(tmp_path, MockProvider([]))
    restarted_sticker = _arrive_sticker(tmp_path, name="re-downloaded-under-new-name.gif")
    await second_engine.process_message(
        _incoming(restarted_sticker, message_id="msg-after-restart"),
        _participants(),
        _GROUP_ID,
    )

    assert second_provider.requests == []
    _assert_model_sees_caption(_model_visible_history(second_engine), _STICKER_CAPTION)
    assert not Path(restarted_sticker["value"]).exists()


@pytest.mark.asyncio
async def test_normal_photo_keeps_its_original(tmp_path):
    """普通图片行为不变：原图留在 image_cache，描述照样进上下文。"""
    engine, provider = _new_engine(tmp_path, MockProvider([_PHOTO_CAPTION]))
    photo = _arrive_photo(tmp_path)
    photo_path = Path(photo["value"])

    await engine.process_message(
        _incoming(photo, message_id="msg-photo"), _participants(), _GROUP_ID
    )

    xml = _model_visible_history(engine)
    assert f'<image type="image" caption="{_PHOTO_CAPTION}"' in xml, xml
    assert len(provider.requests) == 1
    assert photo_path.exists()
    assert Path(f"{photo_path}.url").exists()
    # 描述缓存只有表情包会写。
    assert not (tmp_path / "sticker_captions.json").exists()


@pytest.mark.parametrize("failure", ["empty_caption", "provider_raises"])
@pytest.mark.asyncio
async def test_vision_failure_still_discards_sticker_and_keeps_placeholder(tmp_path, failure):
    """读图失败（返回空 / 抛错）：原图照删、历史退回默认占位、不写脏缓存。"""
    provider = MockProvider([""]) if failure == "empty_caption" else _RaisingVisionProvider()
    engine, provider = _new_engine(tmp_path, provider)
    sticker = _arrive_sticker(tmp_path, with_url_sidecar=True)

    await engine.process_message(
        _incoming(sticker, message_id="msg-broken"), _participants(), _GROUP_ID
    )

    assert len(provider.requests) == 1, "读图确实被尝试过"
    xml = _model_visible_history(engine)
    assert f'caption="{_DEFAULT_STICKER_CAPTION}"' in xml, xml
    assert _STICKER_CAPTION not in xml

    # 读图失败也要删原图：它对模型没有任何可见通道。
    assert list((tmp_path / "sticker_cache").iterdir()) == []
    # 失败不落盘：既没有脏描述，也没有半截 JSON / 残留临时文件。
    assert list(tmp_path.glob("sticker_captions.json*")) == []


@pytest.mark.asyncio
async def test_vision_failure_keeps_existing_caption_cache_intact(tmp_path):
    """读图失败不能把已有的描述缓存写坏（原子写入的意义）。"""
    captions_file = tmp_path / "sticker_captions.json"
    existing = {"0" * 64: "之前那张表情包的描述"}
    captions_file.write_text(json.dumps(existing, ensure_ascii=False), encoding="utf-8")

    engine, provider = _new_engine(tmp_path, _RaisingVisionProvider())
    sticker = _arrive_sticker(tmp_path)
    await engine.process_message(
        _incoming(sticker, message_id="msg-broken"), _participants(), _GROUP_ID
    )

    assert len(provider.requests) == 1
    assert not Path(sticker["value"]).exists()
    assert json.loads(captions_file.read_text(encoding="utf-8")) == existing


@pytest.mark.parametrize("reference", ["plain_path", "file_url"])
@pytest.mark.parametrize("preexisting", [None, {"0" * 64: "之前那张表情包的描述"}])
@pytest.mark.asyncio
async def test_missing_local_sticker_is_not_sent_to_vision(tmp_path, reference, preexisting):
    """本地图片已经不在了（普通路径或 ``file://``）：不烧读图调用，也不写进描述缓存。

    传输层遇到不存在的本地文件会静默丢图，模型手里只剩一句「请识别这张图片」的
    文字提示，多半会顺着提示编一句描述。所以这种输入必须在读图之前就被挡下。
    """
    captions_file = tmp_path / "sticker_captions.json"
    if preexisting is not None:
        captions_file.write_text(json.dumps(preexisting, ensure_ascii=False), encoding="utf-8")

    engine, provider = _new_engine(tmp_path, MockProvider([_STICKER_CAPTION]))
    missing = tmp_path / "sticker_cache" / "long-gone.gif"
    assert not missing.exists(), "前置条件：这张图确实不在了"
    sticker = (
        _file_url_sticker(missing) if reference == "file_url" else _plain_path_sticker(missing)
    )

    await engine.process_message(
        _incoming(sticker, message_id="msg-ghost"),
        _participants(),
        _GROUP_ID,
    )

    assert provider.requests == [], "本地文件已经不在了，不该再送去读图"
    xml = _model_visible_history(engine)
    assert f'caption="{_DEFAULT_STICKER_CAPTION}"' in xml, xml
    assert _STICKER_CAPTION not in xml, "模型不能凭一句文字提示编出描述"

    # 幽灵键（路径/URI 字符串的哈希）绝不能污染描述缓存。
    if preexisting is None:
        assert list(tmp_path.glob("sticker_captions.json*")) == []
    else:
        assert json.loads(captions_file.read_text(encoding="utf-8")) == preexisting


@pytest.mark.asyncio
async def test_existing_file_url_sticker_is_still_read(tmp_path):
    """``file://`` 指向真实存在的图片时照常读图：新增的保护不能误伤合法引用。"""
    real = tmp_path / "stickers" / "my-cat.gif"
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_bytes(_STICKER_BYTES)

    engine, provider = _new_engine(tmp_path, MockProvider([_STICKER_CAPTION]))

    await engine.observe_message(
        _incoming(_file_url_sticker(real), message_id="msg-file-url"),
        _participants(),
        _GROUP_ID,
    )

    assert len(provider.requests) == 1
    _assert_model_sees_caption(_model_visible_history(engine), _STICKER_CAPTION)
    # 引用形式（file:// URI）与本地路径的缓存键不同，这里只钉「描述确实落盘了」。
    payload = json.loads((tmp_path / "sticker_captions.json").read_text(encoding="utf-8"))
    assert list(payload.values()) == [_STICKER_CAPTION]
    # 用户自己的文件不是缓存临时物，照旧不能删。
    assert real.exists()


@pytest.mark.parametrize("broken_payload", ['{"坏掉的": ', '["不是", "字典"]'])
@pytest.mark.asyncio
async def test_corrupt_caption_file_does_not_break_follow_up_messages(tmp_path, broken_payload):
    """sticker_captions.json 坏掉时按空缓存继续，后续消息照常处理并修复文件。"""
    captions_file = tmp_path / "sticker_captions.json"
    captions_file.write_text(broken_payload, encoding="utf-8")

    engine, provider = _new_engine(tmp_path, MockProvider([_STICKER_CAPTION]))
    await engine.process_message(
        _incoming(_arrive_sticker(tmp_path), message_id="msg-1"), _participants(), _GROUP_ID
    )

    assert len(provider.requests) == 1
    _assert_model_sees_caption(_model_visible_history(engine), _STICKER_CAPTION)
    assert json.loads(captions_file.read_text(encoding="utf-8")) == {
        hashlib.sha256(_STICKER_BYTES).hexdigest(): _STICKER_CAPTION
    }

    # 后续普通文本消息也不受影响。
    result = await engine.process_message(
        Message(role="user", content="Luna 在吗？", speaker="Alice"),
        _participants(),
        _GROUP_ID,
    )
    assert isinstance(result, dict)
    assert "Luna 在吗？" in _model_visible_history(engine)


@pytest.mark.asyncio
async def test_sticker_outside_cache_dir_is_never_deleted(tmp_path):
    """用户自己放在 stickers/ 的表情包不是缓存临时物：observe 路径同样不能删。"""
    user_sticker = tmp_path / "stickers" / "my-cat.gif"
    user_sticker.parent.mkdir(parents=True, exist_ok=True)
    user_sticker.write_bytes(b"user-owned-sticker")
    sticker = {
        "type": "image",
        "value": str(user_sticker),
        "file_path": str(user_sticker),
        "sub_type": "1",
    }
    engine, provider = _new_engine(tmp_path, MockProvider([_STICKER_CAPTION]))

    await engine.observe_message(
        _incoming(sticker, message_id="msg-observe"), _participants(), _GROUP_ID
    )

    assert user_sticker.exists(), "用户自己的表情包不能被当成缓存删掉"
    assert len(provider.requests) == 1
    _assert_model_sees_caption(_model_visible_history(engine), _STICKER_CAPTION)
