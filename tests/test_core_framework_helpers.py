from __future__ import annotations

import json
import sqlite3

from sirius_pulse.core.orchestration_store import OrchestrationStore
from sirius_pulse.core.persona_db import PersonaDatabase
from sirius_pulse.core.utils import now_iso, parse_sticker_tags, strip_conversation_history_xml
from sirius_pulse.developer_profiles import metadata_declares_developer
from sirius_pulse.providers.aliyun_bailian import _normalize_aliyun_bailian_base_url
from sirius_pulse.providers.bigmodel import _normalize_bigmodel_base_url
from sirius_pulse.providers.mimo import _normalize_mimo_base_url


def test_developer_profiles_when_metadata_contains_flags_or_roles_then_developer_status_is_detected():
    assert metadata_declares_developer({"is_developer": True}) is True
    assert metadata_declares_developer({"developer": "yes"}) is True
    assert metadata_declares_developer({"developer": "off"}) is False
    assert metadata_declares_developer({"role": "dev"}) is True
    assert metadata_declares_developer({"role": "product-manager"}) is False
    assert metadata_declares_developer({"roles": ["member", "engineer"]}) is True
    assert metadata_declares_developer({"roles": "developer"}) is False
    assert metadata_declares_developer(None) is False


def test_core_utils_when_history_xml_is_present_then_only_conversation_history_blocks_are_removed():
    text = "before <conversation_history id='1'>hidden</conversation_history> after <keep>visible</keep>"

    assert strip_conversation_history_xml(text) == "before  after <keep>visible</keep>"
    assert strip_conversation_history_xml("") == ""
    assert "T" in now_iso()


def test_core_utils_when_sticker_tags_are_present_then_names_are_extracted_and_text_is_cleaned():
    cleaned, names = parse_sticker_tags(
        'hello [STICKERS: "smile", 「wave」, bad] [known] [ignored]',
        sticker_names=["known", "extra"],
    )

    assert cleaned == "hello  [known] [ignored]"
    assert names == ["smile", "wave", "bad"]

    cleaned_keyword, keyword_names = parse_sticker_tags("ok [known] [extra] [third] [fourth]", ["known", "extra", "third"])
    assert cleaned_keyword.split() == ["ok", "[fourth]"]
    assert keyword_names == ["known", "extra", "third"]


def test_core_utils_when_sticker_tag_uses_fullwidth_colon_then_it_is_supported():
    cleaned, names = parse_sticker_tags("[STICKERS： one, two] body")

    assert cleaned == "body"
    assert names == ["one", "two"]


def test_orchestration_store_when_config_is_saved_then_json_round_trips_atomically(tmp_path):
    config = {"unified_model": "model-a", "task_enabled": {"memory_extract": False}}

    assert OrchestrationStore.load(tmp_path) == {}
    OrchestrationStore.save(tmp_path, config)

    path = tmp_path / "engine_state" / "orchestration.json"
    assert OrchestrationStore._path(tmp_path) == path
    assert json.loads(path.read_text(encoding="utf-8")) == config
    assert OrchestrationStore.load(tmp_path) == config
    assert not path.with_suffix(".json.tmp").exists()


def test_orchestration_store_when_file_is_invalid_then_empty_config_is_returned(tmp_path):
    path = tmp_path / "engine_state" / "orchestration.json"
    path.parent.mkdir()
    path.write_text("[1, 2, 3]", encoding="utf-8")

    assert OrchestrationStore.load(tmp_path) == {}

    path.write_text("{broken", encoding="utf-8")
    assert OrchestrationStore.load(tmp_path) == {}


def test_persona_database_when_used_as_context_manager_then_meta_table_exists_and_connection_closes(tmp_path):
    db_path = tmp_path / "persona" / "persona.db"

    with PersonaDatabase(db_path) as database:
        assert database.db_path == db_path
        database.conn.execute("INSERT INTO _meta(key, value) VALUES (?, ?)", ("schema", "1"))
        database.conn.commit()
        assert database.conn.execute("SELECT value FROM _meta WHERE key = 'schema'").fetchone()[0] == "1"

    reopened = sqlite3.connect(db_path)
    try:
        assert reopened.execute("SELECT value FROM _meta WHERE key = 'schema'").fetchone()[0] == "1"
    finally:
        reopened.close()


def test_provider_url_helpers_when_base_urls_vary_then_expected_request_roots_are_returned():
    assert _normalize_aliyun_bailian_base_url("") == "https://dashscope.aliyuncs.com/compatible-mode"
    assert (
        _normalize_aliyun_bailian_base_url("https://dashscope.aliyuncs.com/compatible-mode/v1/")
        == "https://dashscope.aliyuncs.com/compatible-mode"
    )
    assert _normalize_bigmodel_base_url("") == "https://open.bigmodel.cn/api/paas/v4"
    assert _normalize_bigmodel_base_url("https://open.bigmodel.cn/api/paas") == "https://open.bigmodel.cn/api/paas/v4"
    assert _normalize_bigmodel_base_url("https://open.bigmodel.cn") == "https://open.bigmodel.cn/api/paas/v4"
    assert _normalize_mimo_base_url("https://api.xiaomimimo.com/v1/") == "https://api.xiaomimimo.com/v1"
