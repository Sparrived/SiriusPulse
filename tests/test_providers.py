"""Provider 统一基准测试。

所有 OpenAI-compatible 协议 provider 共享相同的响应解析逻辑，
本文件通过参数化测试验证各 provider 的基准行为一致性。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from sirius_pulse.providers.aliyun_bailian import AliyunBailianProvider
from sirius_pulse.providers.base import GenerationRequest
from sirius_pulse.providers.bigmodel import BigModelProvider
from sirius_pulse.providers.deepseek import DeepSeekProvider
from sirius_pulse.providers.mock import MockProvider
from sirius_pulse.providers.openai_compatible import OpenAICompatibleProvider
from sirius_pulse.providers.siliconflow import SiliconFlowProvider
from sirius_pulse.providers.volcengine_ark import VolcengineArkProvider
from sirius_pulse.providers.ytea import YTeaProvider


_PROVIDER_SPECS: list[dict] = [
    {
        "id": "openai_compatible",
        "cls": OpenAICompatibleProvider,
        "init": {"base_url": "https://api.openai.com", "api_key": "test-key"},
        "model": "gpt-4o-mini",
        "expected_url": "https://api.openai.com/v1/chat/completions",
        "has_reasoning": False,
        "thinking_defaults": {},
    },
    {
        "id": "aliyun_bailian",
        "cls": AliyunBailianProvider,
        "init": {"api_key": "test-key"},
        "model": "qwen-plus",
        "expected_url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        "has_reasoning": True,
        "thinking_defaults": {"enable_thinking": False},
    },
    {
        "id": "deepseek",
        "cls": DeepSeekProvider,
        "init": {"api_key": "test-key"},
        "model": "deepseek-chat",
        "expected_url": "https://api.deepseek.com/chat/completions",
        "has_reasoning": True,
        "thinking_defaults": {"thinking": {"type": "disabled"}},
    },
    {
        "id": "bigmodel",
        "cls": BigModelProvider,
        "init": {"api_key": "test-key"},
        "model": "glm-4.6v",
        "expected_url": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        "has_reasoning": True,
        "thinking_defaults": {"thinking": {"type": "disabled"}},
    },
    {
        "id": "siliconflow",
        "cls": SiliconFlowProvider,
        "init": {"api_key": "test-key"},
        "model": "Pro/zai-org/GLM-4.7",
        "expected_url": "https://api.siliconflow.cn/v1/chat/completions",
        "has_reasoning": True,
        "thinking_defaults": {"enable_thinking": False},
    },
    {
        "id": "volcengine_ark",
        "cls": VolcengineArkProvider,
        "init": {"api_key": "test-key"},
        "model": "doubao-seed-2-0-lite-260215",
        "expected_url": "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
        "has_reasoning": True,
        "thinking_defaults": {"thinking": {"type": "disabled"}},
    },
    {
        "id": "ytea",
        "cls": YTeaProvider,
        "init": {"api_key": "test-key"},
        "model": "gpt-4o-mini",
        "expected_url": "https://api.ytea.top/v1/chat/completions",
        "has_reasoning": False,
        "thinking_defaults": {},
    },
]

_PATCH_TARGET = "sirius_pulse.providers.openai_compatible.httpx.AsyncClient"


def _make_request(model: str, *, timeout_seconds: float | None = None) -> GenerationRequest:
    return GenerationRequest(
        model=model,
        system_prompt="你是一个有用的助手",
        messages=[{"role": "user", "content": "你好"}],
        timeout_seconds=timeout_seconds,
    )


def _ids(specs: list[dict]) -> list[str]:
    return [s["id"] for s in specs]


def _create_mock_response(json_data: dict, status_code: int = 200) -> MagicMock:
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_response.text = json.dumps(json_data)
    mock_response.json.return_value = json_data
    mock_response.headers = {"Content-Type": "application/json"}
    return mock_response


def _build_mock_client(mock_response: MagicMock) -> MagicMock:
    mock_client = MagicMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.post = AsyncMock(return_value=mock_response)
    return mock_client


# ---------------------------------------------------------------------------
# 基准 1：纯文本内容返回
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec", _PROVIDER_SPECS, ids=_ids(_PROVIDER_SPECS))
@pytest.mark.asyncio
async def test_provider_returns_plain_content(spec: dict) -> None:
    provider = spec["cls"](**spec["init"])
    mock_client = _build_mock_client(
        _create_mock_response({"choices": [{"message": {"content": "  文本内容  "}}]})
    )
    with patch(_PATCH_TARGET, return_value=mock_client):
        output = await provider.generate_async(_make_request(spec["model"]))
    assert output == "文本内容"


@pytest.mark.parametrize("spec", _PROVIDER_SPECS, ids=_ids(_PROVIDER_SPECS))
@pytest.mark.asyncio
async def test_provider_uses_correct_default_endpoint(spec: dict) -> None:
    provider = spec["cls"](**spec["init"])
    mock_client = _build_mock_client(
        _create_mock_response({"choices": [{"message": {"content": "ok"}}]})
    )
    with patch(_PATCH_TARGET, return_value=mock_client):
        await provider.generate_async(_make_request(spec["model"]))
    mock_client.post.assert_called_once()
    assert mock_client.post.call_args[0][0] == spec["expected_url"]


@pytest.mark.parametrize("spec", _PROVIDER_SPECS, ids=_ids(_PROVIDER_SPECS))
@pytest.mark.asyncio
async def test_provider_applies_expected_thinking_defaults(spec: dict) -> None:
    provider = spec["cls"](**spec["init"])
    mock_client = _build_mock_client(
        _create_mock_response({"choices": [{"message": {"content": "ok"}}]})
    )
    with patch(_PATCH_TARGET, return_value=mock_client):
        await provider.generate_async(_make_request(spec["model"]))
    body = mock_client.post.call_args[1]["content"]
    payload = json.loads(body.decode("utf-8"))
    thinking_defaults = spec["thinking_defaults"]
    if thinking_defaults:
        for key, value in thinking_defaults.items():
            assert payload[key] == value
        return
    assert "enable_thinking" not in payload
    assert "thinking" not in payload


@pytest.mark.parametrize("spec", _PROVIDER_SPECS, ids=_ids(_PROVIDER_SPECS))
@pytest.mark.asyncio
async def test_provider_debug_log_includes_actual_url_and_metadata(
    caplog: pytest.LogCaptureFixture, spec: dict
) -> None:
    provider = spec["cls"](**spec["init"])
    mock_client = _build_mock_client(
        _create_mock_response({"choices": [{"message": {"content": "ok"}}]})
    )
    with (
        caplog.at_level(logging.DEBUG, logger="sirius_pulse.providers.openai_compatible"),
        patch(_PATCH_TARGET, return_value=mock_client),
    ):
        await provider.generate_async(_make_request(spec["model"], timeout_seconds=12.5))
    assert spec["expected_url"] in caplog.text
    assert '"timeout_seconds": 12.5' in caplog.text
    assert '"payload"' in caplog.text
    assert '"provider":' in caplog.text


@pytest.mark.parametrize("spec", _PROVIDER_SPECS, ids=_ids(_PROVIDER_SPECS))
@pytest.mark.asyncio
async def test_provider_uses_request_timeout_override(spec: dict) -> None:
    provider = spec["cls"](**spec["init"])
    mock_client = _build_mock_client(
        _create_mock_response({"choices": [{"message": {"content": "ok"}}]})
    )
    with patch(_PATCH_TARGET, return_value=mock_client) as MockCls:
        await provider.generate_async(_make_request(spec["model"], timeout_seconds=95.0))
    assert MockCls.call_args[1]["timeout"] == 95.0


@pytest.mark.parametrize("spec", _PROVIDER_SPECS, ids=_ids(_PROVIDER_SPECS))
@pytest.mark.asyncio
async def test_provider_falls_back_to_provider_timeout(spec: dict) -> None:
    init = dict(spec["init"])
    init["timeout_seconds"] = 41
    provider = spec["cls"](**init)
    mock_client = _build_mock_client(
        _create_mock_response({"choices": [{"message": {"content": "ok"}}]})
    )
    with patch(_PATCH_TARGET, return_value=mock_client) as MockCls:
        await provider.generate_async(_make_request(spec["model"]))
    assert MockCls.call_args[1]["timeout"] == 41.0


@pytest.mark.asyncio
async def test_bigmodel_provider_normalizes_root_base_url() -> None:
    provider = BigModelProvider(api_key="test-key", base_url="https://open.bigmodel.cn")
    mock_client = _build_mock_client(
        _create_mock_response({"choices": [{"message": {"content": "ok"}}]})
    )
    with patch(_PATCH_TARGET, return_value=mock_client):
        await provider.generate_async(_make_request("glm-4.6v"))
    mock_client.post.assert_called_once()
    assert mock_client.post.call_args[0][0] == "https://open.bigmodel.cn/api/paas/v4/chat/completions"


# ---------------------------------------------------------------------------
# 基准 3：结构化内容列表
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec", _PROVIDER_SPECS, ids=_ids(_PROVIDER_SPECS))
@pytest.mark.asyncio
async def test_provider_accepts_structured_content_list(spec: dict) -> None:
    provider = spec["cls"](**spec["init"])
    mock_client = _build_mock_client(_create_mock_response({
        "choices": [
            {
                "message": {
                    "content": [
                        {"type": "text", "text": "段落A"},
                        {"type": "text", "text": "段落B"},
                    ]
                }
            }
        ]
    }))
    with patch(_PATCH_TARGET, return_value=mock_client):
        output = await provider.generate_async(_make_request(spec["model"]))
    assert output == "段落A\n段落B"


@pytest.mark.parametrize("spec", _PROVIDER_SPECS, ids=_ids(_PROVIDER_SPECS))
@pytest.mark.asyncio
async def test_provider_converts_local_image_path_to_data_url(
    spec: dict, tmp_path: Path
) -> None:
    provider = spec["cls"](**spec["init"])
    image_path = tmp_path / "sample.png"
    image_path.write_bytes(b"fake-png-bytes")
    request = GenerationRequest(
        model=spec["model"],
        system_prompt="你是一个有用的助手",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "请描述这张图片"},
                    {"type": "image_url", "image_url": {"url": str(image_path)}},
                ],
            }
        ],
    )
    mock_client = _build_mock_client(
        _create_mock_response({"choices": [{"message": {"content": "ok"}}]})
    )
    with patch(_PATCH_TARGET, return_value=mock_client):
        await provider.generate_async(request)
    body = mock_client.post.call_args[1]["content"]
    payload = json.loads(body.decode("utf-8"))
    image_url = payload["messages"][1]["content"][1]["image_url"]["url"]
    assert image_url.startswith("data:image/png;base64,")


@pytest.mark.parametrize("spec", _PROVIDER_SPECS, ids=_ids(_PROVIDER_SPECS))
@pytest.mark.asyncio
async def test_provider_surfaces_multimodal_download_hint(spec: dict) -> None:
    provider = spec["cls"](**spec["init"])
    error_response = MagicMock()
    error_response.status_code = 400
    error_response.text = json.dumps({
        "error": {
            "message": "<400> InternalError.Algo.InvalidParameter: Failed to download multimodal content",
            "type": "invalid_request_error",
        }
    })
    error_response.headers = {"Content-Type": "application/json"}
    mock_client = _build_mock_client(error_response)
    with patch(_PATCH_TARGET, return_value=mock_client):
        with pytest.raises(RuntimeError) as exc_info:
            await provider.generate_async(_make_request(spec["model"]))
    message = str(exc_info.value)
    assert "多模态文件下载失败" in message
    assert "Content-Type" in message
    assert "data URL" in message


@pytest.mark.parametrize("spec", _PROVIDER_SPECS, ids=_ids(_PROVIDER_SPECS))
@pytest.mark.asyncio
async def test_provider_falls_back_to_refusal(spec: dict) -> None:
    provider = spec["cls"](**spec["init"])
    mock_client = _build_mock_client(_create_mock_response(
        {"choices": [{"message": {"content": "", "refusal": "拒绝回答"}}]}
    ))
    with patch(_PATCH_TARGET, return_value=mock_client):
        output = await provider.generate_async(_make_request(spec["model"]))
    assert output == "拒绝回答"


_REASONING_SPECS = [s for s in _PROVIDER_SPECS if s["has_reasoning"]]


@pytest.mark.parametrize("spec", _REASONING_SPECS, ids=_ids(_REASONING_SPECS))
@pytest.mark.asyncio
async def test_provider_falls_back_to_reasoning_content(spec: dict) -> None:
    provider = spec["cls"](**spec["init"])
    mock_client = _build_mock_client(_create_mock_response({
        "choices": [
            {
                "message": {
                    "content": "",
                    "reasoning_content": "  推理结果  ",
                }
            }
        ]
    }))
    with patch(_PATCH_TARGET, return_value=mock_client):
        output = await provider.generate_async(_make_request(spec["model"]))
    assert output == "推理结果"


@pytest.mark.parametrize("spec", _PROVIDER_SPECS, ids=_ids(_PROVIDER_SPECS))
@pytest.mark.asyncio
async def test_provider_raises_on_network_failure(spec: dict) -> None:
    from httpx import ConnectError

    provider = spec["cls"](**spec["init"])
    mock_client = _build_mock_client(MagicMock())
    mock_client.post.side_effect = ConnectError("timeout")
    with patch(_PATCH_TARGET, return_value=mock_client):
        with pytest.raises(RuntimeError):
            await provider.generate_async(_make_request(spec["model"]))


# ---------------------------------------------------------------------------
# MockProvider 基准
# ---------------------------------------------------------------------------


class TestMockProvider:
    @pytest.mark.asyncio
    async def test_consumes_predefined_responses(self) -> None:
        provider = MockProvider(responses=["x", "y"])
        req = _make_request("mock-model")
        assert await provider.generate_async(req) == "x"
        assert await provider.generate_async(req) == "y"
        assert (await provider.generate_async(req)).startswith("[mock]")
        assert len(provider.requests) == 3
