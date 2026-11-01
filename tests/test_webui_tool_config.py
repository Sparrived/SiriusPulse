"""WebUI Tool 配置 API：已保存的密钥不回显给浏览器，原样回传掩码时保留原值。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiohttp.test_utils import make_mocked_request

from sirius_pulse.webui.server_tool_api import (
    api_persona_tool_config_get,
    api_persona_tool_config_post,
    api_persona_tools_get,
)

MASK = "********"


class _FakeJsonRequest:
    def __init__(self, payload: dict[str, object], tool_name: str) -> None:
        self._payload = payload
        self.match_info = {"tool_name": tool_name}

    async def json(self) -> dict[str, object]:
        return self._payload


def _write_store(persona_dir: Path, tool_name: str, data: dict[str, object]) -> None:
    store = persona_dir / "tool_data" / f"{tool_name}.json"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps(data), encoding="utf-8")


def _read_store(persona_dir: Path, tool_name: str) -> dict[str, object]:
    return json.loads((persona_dir / "tool_data" / f"{tool_name}.json").read_text("utf-8"))


async def _get_config(persona_dir: Path, tool_name: str) -> dict[str, object]:
    request = make_mocked_request(
        "GET", f"/api/persona/tools/{tool_name}/config", match_info={"tool_name": tool_name}
    )
    response = await api_persona_tool_config_get(request, persona_dir)
    assert response.status == 200
    return json.loads(response.text)


async def _save_config(persona_dir: Path, tool_name: str, config: dict[str, object]) -> None:
    response = await api_persona_tool_config_post(
        _FakeJsonRequest({"config": config, "enabled": True}, tool_name), persona_dir
    )
    assert response.status == 200


MASTER = "interaction_with_master"


@pytest.fixture
def persona_dir(tmp_path: Path) -> Path:
    _write_store(tmp_path, "web_lookup", {"tavily_api_key": "tvly-real-secret"})
    _write_store(
        tmp_path,
        MASTER,
        {
            "public_status_token": "pst-real",
            "base_url": "https://status.example",
            "timeout_seconds": 5,
            "_last_poll_at": 123,
        },
    )
    return tmp_path


@pytest.mark.asyncio
async def test_saved_tool_secrets_are_never_sent_to_the_browser(persona_dir: Path) -> None:
    detail = await _get_config(persona_dir, MASTER)
    assert detail["config"] == {
        "public_status_token": MASK,
        "base_url": "https://status.example",
        "timeout_seconds": 5,
    }

    response = await api_persona_tools_get(
        make_mocked_request("GET", "/api/persona/tools"), persona_dir
    )
    body = response.text
    assert "tvly-real-secret" not in body
    assert "pst-real" not in body
    tools = {tool["name"]: tool for tool in json.loads(body)["tools"]}
    assert tools["web_lookup"]["config"]["tavily_api_key"] == MASK
    assert tools[MASTER]["config"]["public_status_token"] == MASK


@pytest.mark.asyncio
async def test_unset_tool_secret_reads_as_empty_not_as_saved(tmp_path: Path) -> None:
    _write_store(tmp_path, "web_lookup", {"tavily_api_key": ""})

    detail = await _get_config(tmp_path, "web_lookup")

    assert detail["config"]["tavily_api_key"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("submitted", [MASK, ""])
async def test_resubmitting_the_form_keeps_the_stored_secret(
    persona_dir: Path, submitted: str
) -> None:
    form = (await _get_config(persona_dir, MASTER))["config"]
    form["public_status_token"] = submitted
    form["base_url"] = "https://status.example/v2"
    form["timeout_seconds"] = 9

    await _save_config(persona_dir, MASTER, form)

    stored = _read_store(persona_dir, MASTER)
    assert stored["public_status_token"] == "pst-real"
    assert stored["_last_poll_at"] == 123
    reloaded = (await _get_config(persona_dir, MASTER))["config"]
    assert reloaded["base_url"] == "https://status.example/v2"
    assert reloaded["timeout_seconds"] == 9


@pytest.mark.asyncio
async def test_entering_a_new_secret_replaces_the_stored_one(persona_dir: Path) -> None:
    await _save_config(persona_dir, "web_lookup", {"tavily_api_key": "tvly-new"})

    assert _read_store(persona_dir, "web_lookup")["tavily_api_key"] == "tvly-new"
    assert (await _get_config(persona_dir, "web_lookup"))["config"]["tavily_api_key"] == MASK


@pytest.mark.asyncio
async def test_masked_secret_never_lands_on_disk_for_a_fresh_tool(tmp_path: Path) -> None:
    await _save_config(tmp_path, MASTER, {"public_status_token": MASK, "timeout_seconds": 5})

    stored = _read_store(tmp_path, MASTER)
    assert "public_status_token" not in stored
    assert stored["timeout_seconds"] == 5


@pytest.mark.asyncio
async def test_undeclared_extra_keys_follow_the_same_secret_rule(persona_dir: Path) -> None:
    _write_store(
        persona_dir,
        "web_lookup",
        {"tavily_api_key": "tvly-real-secret", "proxy_token": "px-real", "region": "cn"},
    )

    # 背包抽屉会把未声明的额外键与表单合并后一起提交
    await _save_config(
        persona_dir,
        "web_lookup",
        {"tavily_api_key": MASK, "proxy_token": MASK, "region": "jp", "note": "hi"},
    )

    stored = _read_store(persona_dir, "web_lookup")
    assert stored["tavily_api_key"] == "tvly-real-secret"
    assert stored["proxy_token"] == "px-real"
    assert stored["region"] == "jp"
    assert stored["note"] == "hi"

    await _save_config(persona_dir, "web_lookup", {"proxy_token": "px-new"})
    assert _read_store(persona_dir, "web_lookup")["proxy_token"] == "px-new"
