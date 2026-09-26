from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from sirius_pulse.config import TokenUsageRecord
from sirius_pulse.plugins.models import PluginDefinition, PluginParameterDef, PluginPermissionDef
from sirius_pulse.token.token_store import TokenUsageStore
from sirius_pulse.utils.json_io import atomic_write_json
from sirius_pulse.webui import persona_manager_api as persona_manager
from sirius_pulse.webui.app_keys import DATA_DIR_KEY
from sirius_pulse.webui.memory_api import api_persona_tokens_get
from sirius_pulse.webui.persona_api import (
    _resolve_persona_log_file,
    api_persona_logs_get,
    api_system_logs_get,
)
from sirius_pulse.webui.routes import WEBUI_ROUTES
from sirius_pulse.webui.server import DELEGATED_HANDLERS, WebUIServer
from sirius_pulse.webui.server_plugin_api import (
    MaskedSecretUpdateError,
    _effective_permissions,
    _masked_parameter,
    _masked_settings,
    _request_plugin_reload,
    _settings_update_without_masked_secrets,
    _validate_settings_schema,
    api_plugin_config_get,
    api_plugin_config_post,
    api_plugin_detail_get,
    api_plugin_setting_delete,
    api_plugin_setting_post,
    api_plugin_settings_get,
    api_plugin_settings_post,
    api_plugin_toggle,
    api_plugins_get,
)


def _route_snapshot(app: web.Application) -> set[tuple[str, str]]:
    routes: set[tuple[str, str]] = set()
    for route in app.router.routes():
        resource = route.resource
        path = getattr(resource, "canonical", None)
        if path is None:
            continue
        routes.add((route.method, path))
    return routes


def test_webui_form_constructor_clones_settings_and_keeps_stable_object_row_identity():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is not installed")
    source_path = (
        Path(__file__).resolve().parents[1] / "sirius_pulse" / "webui" / "static" / "components.js"
    )
    script = f"""
      import assert from 'node:assert/strict';
      import {{
        DynamicConfigForm,
      }} from {json.dumps(source_path.as_uri())};

      // Settings are cloned immediately; later caller mutations must not leak.
      const original = {{ tags: ['a'], options: {{ flags: [true] }} }};
      const form = new DynamicConfigForm({{
        parameters: [
          {{ name: 'tags', type: 'list' }},
          {{ name: 'options', type: 'json' }},
          {{ name: 'schedules', type: 'schedule' }},
          {{
            name: 'sources',
            type: 'object_array',
            fields: [
              {{ name: 'id', type: 'str', identity: true, default: '' }},
              {{ name: 'secret_key', type: 'password' }},
            ],
          }},
        ],
        settings: {{
          ...original,
          schedules: [{{ time: '08:00', duration: 90 }}],
          sources: [{{ id: 'a', secret_key: 'must-be-dropped' }}],
        }},
      }});
      original.tags.push('b');
      original.options.flags.push(false);
      assert.deepEqual(JSON.parse(JSON.stringify(form.settings.tags)), ['a']);
      assert.deepEqual(JSON.parse(JSON.stringify(form.settings.options)), {{ flags: [true] }});

      // Declared schedule parameters are copied without aliasing the input.
      const declared = form._scheduleData.schedules;
      declared[0].time = '23:59';
      assert.equal(form.settings.schedules[0].time, '08:00');

      // Legacy schedule-shaped settings still initialize the schedule editor.
      const legacy = new DynamicConfigForm({{
        parameters: [],
        settings: {{ schedule: [{{ time: '22:00', duration: 1440 }}] }},
      }});
      assert.deepEqual(JSON.parse(JSON.stringify(legacy._scheduleData.schedule)), [
        {{ time: '22:00', duration: 1440 }},
      ]);

      // Object-array rows are normalized against the executable field
      // contract: secret fields drop, stable row identity survives rerenders.
      const rows = form.settings.sources;
      assert.deepEqual(JSON.parse(JSON.stringify(rows)), [{{ id: 'a' }}]);
      const firstId = form._objectRowStateId(rows[0], 0);
      assert.equal(form._objectRowStateId(rows[0], 0), firstId);
      assert.equal(form._objectRowIndex('sources', firstId, 99), 0);
    """

    completed = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        check=False,
        text=True,
        timeout=5,
    )
    assert completed.returncode == 0, completed.stderr


def test_webui_object_array_defaults_are_safe_independent_and_persistable():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is not installed")
    source_path = (
        Path(__file__).resolve().parents[1] / "sirius_pulse" / "webui" / "static" / "components.js"
    )
    script = f"""
      import assert from 'node:assert/strict';
      import {{
        createObjectArrayItem,
        DynamicConfigForm,
        parseConfigNumber,
      }} from {json.dumps(source_path.as_uri())};

      const fields = [
        {{ name: 'name', type: 'str', default: '' }},
        {{ name: 'enabled', type: 'bool', default: true }},
        {{ name: 'disabled', type: 'boolean', default: false }},
        {{ name: 'timeout', type: 'number', default: 0 }},
        {{ name: 'nullable', type: 'number' }},
        {{ name: 'groups', type: 'list', default: ['base'] }},
        {{ name: 'options', type: 'json', default: {{ flags: [false] }} }},
        {{ name: 'repository_token', type: 'password', default: 'row-secret' }},
        {{ name: 'clientSecret', type: 'str', default: 'mislabeled-secret' }},
        {{ name: '__proto__', type: 'json', default: {{ polluted: true }} }},
        {{ name: 'prototype', type: 'str', default: 'bad' }},
        {{ name: 'constructor', type: 'str', default: 'bad' }},
      ];
      const first = createObjectArrayItem(fields);
      const second = createObjectArrayItem(fields);
      first.groups.push('changed');
      first.options.flags.push(true);

      assert.equal(Object.getPrototypeOf(first), null);
      assert.deepEqual(JSON.parse(JSON.stringify(first)), {{
        name: '',
        enabled: true,
        disabled: false,
        timeout: 0,
        groups: ['base', 'changed'],
        options: {{ flags: [false, true] }},
      }});
      assert.deepEqual(JSON.parse(JSON.stringify(second)), {{
        name: '',
        enabled: true,
        disabled: false,
        timeout: 0,
        groups: ['base'],
        options: {{ flags: [false] }},
      }});
      assert.equal(Object.prototype.polluted, undefined);
      assert.equal(parseConfigNumber(''), undefined);
      assert.equal(parseConfigNumber('0'), 0);

      globalThis.document = {{
        getElementById: () => ({{ querySelectorAll: () => [] }}),
      }};
      const form = new DynamicConfigForm({{
        containerId: 'plugin-form',
        parameters: [{{
          name: 'repos',
          type: 'object_array',
          fields,
          default: [{{
            name: 'starter',
            enabled: false,
            repository_token: '********',
          }}],
        }}],
        settings: {{}},
      }});
      const collected = form.collectValues();
      assert.equal(collected.repos[0].name, 'starter');
      assert.equal(collected.repos[0].enabled, false);
      assert.equal(collected.repos[0].timeout, 0);
      assert.equal('nullable' in collected.repos[0], false);
      assert.equal('repository_token' in collected.repos[0], false);
      assert.equal('clientSecret' in collected.repos[0], false);
      assert.equal('__proto__' in collected.repos[0], false);
      assert.equal(Object.getPrototypeOf(collected.repos[0]), null);

      const emptyDefaultForm = new DynamicConfigForm({{
        containerId: 'plugin-form',
        parameters: [{{ name: 'sources', type: 'object_array', fields, default: [] }}],
        settings: {{}},
      }});
      const emptyCollected = emptyDefaultForm.collectValues();
      assert.equal('sources' in emptyCollected, false);

      const mixedCaseForm = new DynamicConfigForm({{
        containerId: 'plugin-form',
        parameters: [{{
          name: 'sources',
          type: 'OBJECT_ARRAY',
          fields: [{{ name: 'id', type: 'str' }}],
        }}],
        settings: {{ sources: [{{ id: 'alpha' }}] }},
      }});
      const mixedCaseCollected = mixedCaseForm.collectValues();
      assert.deepEqual(JSON.parse(JSON.stringify(mixedCaseCollected.sources)), [{{ id: 'alpha' }}]);
    """

    completed = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        check=False,
        text=True,
        timeout=5,
    )

    assert completed.returncode == 0, completed.stderr


def test_webui_plugin_ui_schema_browser_boundary_fails_closed():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is not installed")
    source_path = (
        Path(__file__).resolve().parents[1]
        / "sirius_pulse"
        / "webui"
        / "static"
        / "plugin-ui-schema.js"
    )
    script = f"""
      import assert from 'node:assert/strict';
      import {{ normalizePluginUISchema }} from {json.dumps(source_path.as_uri())};

      const parameters = [
        {{
          name: 'sources',
          type: 'object_array',
          fields: [
            {{ name: 'id', type: 'str', identity: true }},
            {{ name: 'display_name', type: 'str' }},
            {{ name: 'enabled', type: 'bool' }},
            {{ name: 'timeout', type: 'int' }},
            {{ name: 'api_token', type: 'password' }},
          ],
        }},
        {{ name: 'poll_seconds', type: 'int' }},
      ];
      const valid = {{
        version: 1,
        layout: 'wide',
        title: '多站监控',
        sections: [
          {{ id: 'sites', parameters: ['sources'], columns: 1, collapsed: false, tone: 'accent' }},
          {{ id: 'runtime', parameters: ['poll_seconds'], columns: 2,
             collapsed: false, tone: 'default' }},
        ],
        parameters: {{
          sources: {{
            label: '站点列表',
            add_label: '添加站点',
            item_title_field: 'display_name',
            item_fallback_field: 'id',
            item_badge_field: 'id',
            item_status_field: 'enabled',
            fields: {{
              id: {{ label: '站点 ID', widget: 'code', span: 6 }},
              display_name: {{ label: '显示名称', span: 6 }},
              enabled: {{ widget: 'switch', true_label: '监控中', false_label: '已停用' }},
              timeout: {{ unit: '秒' }},
              api_token: {{ label: '环境凭据' }},
            }},
            fieldsets: [
              {{ id: 'identity', fields: ['enabled', 'id', 'display_name'], collapsed: false }},
              {{ id: 'network', fields: ['timeout', 'api_token'], collapsed: true }},
            ],
          }},
          poll_seconds: {{ label: '轮询间隔', unit: '秒', span: 6 }},
        }},
      }};
      const clone = value => JSON.parse(JSON.stringify(value));
      const mutate = callback => {{
        const value = clone(valid);
        callback(value);
        return value;
      }};

      const normalized = normalizePluginUISchema(valid, parameters);
      assert.deepEqual(normalized, valid);
      assert.notEqual(normalized, valid);
      assert.equal(normalizePluginUISchema(null, parameters), null);
      assert.equal(
         normalizePluginUISchema(mutate(value => delete value.version), parameters),
         null,
       );
      assert.equal(
         normalizePluginUISchema(mutate(value => value.css = 'body{{display:none}}'), parameters),
         null,
       );
      assert.equal(normalizePluginUISchema(
        mutate(value => value.sections[0].parameters.push('missing')),
        parameters,
      ), null);
      assert.equal(normalizePluginUISchema(
        mutate(value => value.sections[1].parameters.push('sources')),
        parameters,
      ), null);
      assert.equal(normalizePluginUISchema(
        mutate(value => value.parameters.poll_seconds.widget = 'text'),
        parameters,
      ), null);
      assert.equal(normalizePluginUISchema(
        mutate(value => value.parameters.sources.item_title_field = 'api_token'),
        parameters,
      ), null);
      assert.equal(normalizePluginUISchema(
        mutate(value => value.title = '<img src=x onerror=alert(1)>'),
        parameters,
      ), null);
      assert.equal(normalizePluginUISchema(
        mutate(value => (
          value.parameters.sources.help = 'https://user:password@host.invalid/config'
        )),
        parameters,
      ), null);
      assert.equal(normalizePluginUISchema(
        mutate(value => value.title = 'Authorization: Bearer sk-live-example-123456'),
        parameters,
      ), null);

      assert.equal(normalizePluginUISchema({{
        version: 1,
        parameters: {{ sources: {{ item_title_field: 'not_declared' }} }},
      }}, parameters), null);
      assert.equal(normalizePluginUISchema({{
        version: 1,
        parameters: {{ sources: {{ fields: {{ not_declared: {{ label: 'x' }} }} }} }},
      }}, parameters), null);
      assert.equal(normalizePluginUISchema({{
        version: 1,
        parameters: {{ unknown: {{ label: 'x' }} }},
      }}, parameters), null);

      const inherited = clone(valid);
      delete inherited.version;
      Object.setPrototypeOf(inherited, {{ version: 1 }});
      assert.equal(normalizePluginUISchema(inherited, parameters), null);

      const accessor = clone(valid);
      Object.defineProperty(
        accessor,
        'title',
        {{ get() {{ throw new Error('must not execute'); }} }},
      );
      assert.equal(normalizePluginUISchema(accessor, parameters), null);

      const polluted = JSON.parse(JSON.stringify(valid));
      polluted.parameters.sources.fields = JSON.parse('{{"__proto__":{{"label":"bad"}}}}');
      assert.equal(normalizePluginUISchema(polluted, parameters), null);
      assert.equal(Object.prototype.label, undefined);

      const shared = {{ label: '共享' }};
      const sharedSchema = clone(valid);
      sharedSchema.parameters.poll_seconds = shared;
      sharedSchema.parameters.sources.fields.id = shared;
      assert.equal(normalizePluginUISchema(sharedSchema, parameters), null);

      const cyclic = clone(valid);
      cyclic.self = cyclic;
      assert.equal(normalizePluginUISchema(cyclic, parameters), null);

      class Parameter {{ constructor() {{ this.name = 'poll_seconds'; this.type = 'int'; }} }}
      assert.equal(normalizePluginUISchema(valid, [new Parameter()]), null);
    """

    completed = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        check=False,
        text=True,
        timeout=5,
    )

    assert completed.returncode == 0, completed.stderr


def _visual_plugin_definition() -> PluginDefinition:
    return PluginDefinition(
        name="visual_demo",
        display_name="可视化插件",
        parameters=[
            PluginParameterDef(
                name="sources",
                type="object_array",
                default=[],
                fields=[
                    {"name": "id", "type": "str", "required": True, "identity": True},
                    {"name": "display_name", "type": "str"},
                    {"name": "enabled", "type": "bool", "default": True},
                ],
            ),
            PluginParameterDef(name="poll_seconds", type="int", default=300, minimum=30),
        ],
        ui_schema={
            "version": 1,
            "layout": "wide",
            "title": "多站监控",
            "sections": [
                {
                    "id": "sites",
                    "title": "站点",
                    "parameters": ["sources"],
                    "columns": 1,
                    "collapsed": False,
                    "tone": "accent",
                },
                {
                    "id": "runtime",
                    "title": "运行参数",
                    "parameters": ["poll_seconds"],
                    "columns": 2,
                    "collapsed": False,
                    "tone": "default",
                },
            ],
            "parameters": {
                "sources": {
                    "label": "站点列表",
                    "item_title_field": "display_name",
                    "item_fallback_field": "id",
                    "item_badge_field": "id",
                    "item_status_field": "enabled",
                    "fields": {
                        "id": {"label": "站点 ID"},
                        "display_name": {"label": "显示名称"},
                        "enabled": {
                            "label": "状态",
                            "widget": "switch",
                            "true_label": "监控中",
                            "false_label": "已停用",
                        },
                    },
                    "fieldsets": [
                        {
                            "id": "identity",
                            "title": "身份",
                            "fields": ["enabled", "id", "display_name"],
                            "collapsed": False,
                        }
                    ],
                },
                "poll_seconds": {"label": "轮询间隔", "unit": "秒"},
            },
        },
    )


def _demo_plugin_definition() -> PluginDefinition:
    return PluginDefinition(
        name="demo",
        parameters=[
            PluginParameterDef(name="api_key", type="password"),
            PluginParameterDef(name="label", type="str"),
            PluginParameterDef(
                name="credentials",
                type="object",
                fields=[
                    {"name": "access_token", "type": "password"},
                    {"name": "label", "type": "str"},
                ],
            ),
            PluginParameterDef(
                name="repos",
                type="object_array",
                fields=[
                    {"name": "name", "type": "str"},
                    {"name": "repository_token", "type": "password"},
                ],
            ),
        ],
    )


class _FakeJsonRequest:
    """最小 aiohttp 请求替身：只提供 await request.json()。"""

    def __init__(
        self,
        payload: dict[str, object],
        match_info: dict[str, str] | None = None,
    ) -> None:
        self._payload = payload
        self.match_info = match_info or {}

    async def json(self) -> dict[str, object]:
        return self._payload


def test_webui_plugin_settings_mask_secrets_and_preserve_masked_updates():
    settings = {
        "password": "real-password",
        "api_key": "real-key",
        "nested": {"accessToken": "real-token", "label": "safe"},
        "repos": [{"name": "demo", "repository_token": "repo-token"}],
    }

    masked = _masked_settings(settings)
    assert masked == {
        "password": "********",
        "api_key": "********",
        "nested": {"accessToken": "********", "label": "safe"},
        "repos": [{"name": "demo", "repository_token": "********"}],
    }

    updated = _settings_update_without_masked_secrets(
        {
            "password": "********",
            "api_key": "",
            "nested": {"accessToken": "********", "label": "updated"},
            "repos": [{"name": "demo", "repository_token": "********"}],
        },
        existing=settings,
    )
    assert updated == {
        "password": "real-password",
        "api_key": "real-key",
        "nested": {"accessToken": "real-token", "label": "updated"},
        "repos": [{"name": "demo", "repository_token": "repo-token"}],
    }
    with pytest.raises(MaskedSecretUpdateError):
        _settings_update_without_masked_secrets(
            {"repos": [{"name": "renamed", "repository_token": "********"}]},
            existing=settings,
        )


def test_webui_plugin_parameter_masks_password_defaults_and_nested_fields():
    parameter = SimpleNamespace(
        name="credentials",
        type="object_array",
        description="",
        required=False,
        default=[
            {
                "repository_token": "default-secret",
                "label": "starter",
                "__proto__": {"polluted": True},
            }
        ],
        choices=None,
        fields=[
            {"name": "repository_token", "type": "password", "default": "secret"},
            {"name": "label", "type": "str", "default": "demo"},
            {"name": "__proto__", "type": "json", "default": {"polluted": True}},
        ],
        group="",
    )

    masked = _masked_parameter(parameter)
    assert masked == {"name": "", "type": "invalid", "fields": None}
    assert "default-secret" not in json.dumps(masked, ensure_ascii=False)
    assert "secret" not in json.dumps(masked, ensure_ascii=False)


@pytest.mark.asyncio
async def test_webui_plugin_settings_post_rejects_plaintext_secret_without_persisting(tmp_path):
    manager = SimpleNamespace(
        data_path=tmp_path / "data",
        plugin_definitions={"demo": _demo_plugin_definition()},
    )
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    config_path = plugins_dir / "_config.json"
    atomic_write_json(
        config_path,
        {
            "demo": {
                "enabled": True,
                "permissions": {},
                "settings": {"api_key": "stored-secret", "label": "old"},
            }
        },
    )
    before = config_path.read_text(encoding="utf-8")
    request = _FakeJsonRequest(
        {"settings": {"api_key": "new-secret", "label": "new"}},
        {"plugin_name": "demo"},
    )

    response = await api_plugin_settings_post(request, manager)

    assert response.status == 400
    assert "new-secret" not in response.text
    assert config_path.read_text(encoding="utf-8") == before
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["demo"]["settings"] == {"api_key": "stored-secret", "label": "old"}


@pytest.mark.asyncio
async def test_webui_plugin_full_form_save_retains_browser_omitted_sensitive_settings(tmp_path):
    definition = PluginDefinition(
        name="safe_form",
        parameters=[
            PluginParameterDef(name="api_key", type="password"),
            PluginParameterDef(name="label", type="str"),
            PluginParameterDef(name="ordinary_removed", type="str"),
            PluginParameterDef(name="payload", type="json"),
            PluginParameterDef(
                name="credentials",
                type="object",
                fields=[
                    {"name": "access_token", "type": "password"},
                    {"name": "label", "type": "str"},
                ],
            ),
            PluginParameterDef(
                name="repos",
                type="object_array",
                fields=[
                    {"name": "owner", "type": "str", "required": True},
                    {"name": "repo", "type": "str", "required": True},
                    {"name": "repository_token", "type": "password", "required": True},
                ],
            ),
        ],
    )
    manager = SimpleNamespace(
        data_path=tmp_path / "data",
        plugin_definitions={"safe_form": definition},
    )
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    config_path = plugins_dir / "_config.json"
    atomic_write_json(
        config_path,
        {
            "safe_form": {
                "enabled": True,
                "permissions": {},
                "settings": {
                    "api_key": "top-secret",
                    "label": "old",
                    "ordinary_removed": "remove me",
                    "payload": {"safe": [1, 2]},
                    "credentials": {"access_token": "object-secret", "label": "stored"},
                    "repos": [
                        {
                            "owner": "alpha",
                            "repo": "one",
                            "repository_token": "alpha-secret",
                        },
                        {
                            "owner": "beta",
                            "repo": "two",
                            "repository_token": "beta-secret",
                        },
                    ],
                },
            }
        },
    )

    response = await api_plugin_settings_post(
        _FakeJsonRequest(
            {
                "settings": {
                    "label": "updated",
                    "repos": [
                        {"owner": "beta", "repo": "two"},
                        {"owner": "alpha", "repo": "one"},
                        {"owner": "gamma", "repo": "three"},
                    ],
                }
            },
            {"plugin_name": "safe_form"},
        ),
        manager,
    )
    payload = json.loads(response.text)
    saved = json.loads(config_path.read_text(encoding="utf-8"))["safe_form"]["settings"]

    assert response.status == 200
    assert saved == {
        "api_key": "top-secret",
        "label": "updated",
        "payload": {"safe": [1, 2]},
        "credentials": {"access_token": "object-secret", "label": "stored"},
        "repos": [
            {"owner": "beta", "repo": "two", "repository_token": "beta-secret"},
            {"owner": "alpha", "repo": "one", "repository_token": "alpha-secret"},
            {"owner": "gamma", "repo": "three"},
        ],
    }
    assert "ordinary_removed" not in saved
    assert payload["settings"] == {
        "api_key": "********",
        "label": "updated",
        "payload": {"safe": [1, 2]},
        "credentials": {"access_token": "********", "label": "stored"},
        "repos": [
            {"owner": "beta", "repo": "two", "repository_token": "********"},
            {"owner": "alpha", "repo": "one", "repository_token": "********"},
            {"owner": "gamma", "repo": "three"},
        ],
    }
    for secret in ("top-secret", "object-secret", "alpha-secret", "beta-secret"):
        assert secret not in response.text


@pytest.mark.asyncio
async def test_webui_plugin_setting_post_handles_nested_secrets_and_masks_response(tmp_path):
    manager = SimpleNamespace(
        data_path=tmp_path / "data",
        plugin_definitions={"demo": _demo_plugin_definition()},
    )
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    config_path = plugins_dir / "_config.json"
    atomic_write_json(
        config_path,
        {
            "demo": {
                "enabled": True,
                "permissions": {},
                "settings": {
                    "credentials": {"access_token": "object-secret", "label": "old"},
                    "repos": [{"name": "demo", "repository_token": "list-secret"}],
                },
            }
        },
    )

    rejected = await api_plugin_setting_post(
        _FakeJsonRequest(
            {"value": {"access_token": "new-object-secret", "label": "new"}},
            {"plugin_name": "demo", "key": "credentials"},
        ),
        manager,
    )
    assert rejected.status == 400
    assert "new-object-secret" not in rejected.text
    assert "list-secret" not in rejected.text

    rejected_list = await api_plugin_setting_post(
        _FakeJsonRequest(
            {"value": [{"name": "new", "repository_token": "new-list-secret"}]},
            {"plugin_name": "demo", "key": "repos"},
        ),
        manager,
    )
    assert rejected_list.status == 400
    assert "new-list-secret" not in rejected_list.text

    accepted_single = await api_plugin_setting_post(
        _FakeJsonRequest(
            {"value": {"access_token": "********", "label": "single"}},
            {"plugin_name": "demo", "key": "credentials"},
        ),
        manager,
    )
    single_payload = json.loads(accepted_single.text)
    single_saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert accepted_single.status == 200
    assert "object-secret" not in accepted_single.text
    assert single_payload["value"] == {"access_token": "********", "label": "single"}
    assert single_saved["demo"]["settings"]["credentials"] == {
        "access_token": "object-secret",
        "label": "single",
    }

    accepted = await api_plugin_settings_post(
        _FakeJsonRequest(
            {
                "settings": {
                    "credentials": {"access_token": "********", "label": "new"},
                    "repos": [{"name": "demo", "repository_token": "********"}],
                }
            },
            {"plugin_name": "demo"},
        ),
        manager,
    )
    payload = json.loads(accepted.text)
    saved = json.loads(config_path.read_text(encoding="utf-8"))

    assert accepted.status == 200
    assert "object-secret" not in accepted.text
    assert "list-secret" not in accepted.text
    assert payload["settings"] == {
        "credentials": {"access_token": "********", "label": "new"},
        "repos": [{"name": "demo", "repository_token": "********"}],
    }
    assert saved["demo"]["settings"] == {
        "credentials": {"access_token": "object-secret", "label": "new"},
        "repos": [{"name": "demo", "repository_token": "list-secret"}],
    }


@pytest.mark.asyncio
async def test_webui_json_and_mixed_case_object_array_settings_are_editable_and_validated(
    tmp_path,
):
    definition = PluginDefinition(
        name="json_editor",
        parameters=[
            PluginParameterDef(name="payload", type="json", required=True),
            PluginParameterDef(name="groups", type="list"),
            PluginParameterDef(
                name="sources",
                type="OBJECT_ARRAY",
                fields=[{"name": "id", "type": "str", "required": True, "identity": True}],
            ),
        ],
    )
    manager = SimpleNamespace(
        data_path=tmp_path / "data",
        plugin_definitions={"json_editor": definition},
    )
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    config_path = plugins_dir / "_config.json"
    atomic_write_json(
        config_path,
        {
            "json_editor": {
                "enabled": True,
                "permissions": {},
                "settings": {
                    "payload": {"safe": [1]},
                    "groups": ["base"],
                    "sources": [{"id": "alpha"}],
                },
            }
        },
    )

    accepted = await api_plugin_settings_post(
        _FakeJsonRequest(
            {
                "settings": {
                    "payload": {"safe": [2], "label": "updated"},
                    "groups": ["changed"],
                    "sources": [{"id": "beta"}],
                }
            },
            {"plugin_name": "json_editor"},
        ),
        manager,
    )
    saved = json.loads(config_path.read_text(encoding="utf-8"))["json_editor"]["settings"]
    assert accepted.status == 200
    assert saved == {
        "payload": {"safe": [2], "label": "updated"},
        "groups": ["changed"],
        "sources": [{"id": "beta"}],
    }

    direct = await api_plugin_setting_post(
        _FakeJsonRequest(
            {"value": [{"id": "gamma"}]},
            {"plugin_name": "json_editor", "key": "sources"},
        ),
        manager,
    )
    assert direct.status == 200
    assert json.loads(config_path.read_text(encoding="utf-8"))["json_editor"]["settings"][
        "sources"
    ] == [{"id": "gamma"}]

    before_invalid = config_path.read_bytes()
    invalid_shape = await api_plugin_settings_post(
        _FakeJsonRequest(
            {"settings": {"payload": ["not", "an", "object"]}},
            {"plugin_name": "json_editor"},
        ),
        manager,
    )
    invalid_blank = await api_plugin_settings_post(
        _FakeJsonRequest(
            {"settings": {"groups": ["  "]}},
            {"plugin_name": "json_editor"},
        ),
        manager,
    )
    assert invalid_shape.status == 400
    assert invalid_blank.status == 400
    assert config_path.read_bytes() == before_invalid


@pytest.mark.asyncio
async def test_webui_schedule_parameters_validate_time_and_duration_bounds(tmp_path):
    definition = PluginDefinition(
        name="scheduled_plugin",
        parameters=[
            PluginParameterDef(name="cron", type="schedule"),
            PluginParameterDef(
                name="runtime",
                type="object_array",
                fields=[{"name": "id", "type": "str", "identity": True}],
            ),
        ],
    )
    manager = SimpleNamespace(
        data_path=tmp_path / "data",
        plugin_definitions={"scheduled_plugin": definition},
    )
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    config_path = plugins_dir / "_config.json"
    atomic_write_json(
        config_path,
        {
            "scheduled_plugin": {
                "enabled": True,
                "permissions": {},
                "settings": {"cron": [{"time": "08:30", "duration": 30}]},
            }
        },
    )

    accepted = await api_plugin_settings_post(
        _FakeJsonRequest(
            {
                "settings": {
                    "cron": [
                        {"time": "22:05", "duration": 1440},
                        {"time": "00:00", "duration": 1},
                    ],
                    "runtime": [{"id": "alpha"}],
                }
            },
            {"plugin_name": "scheduled_plugin"},
        ),
        manager,
    )
    assert accepted.status == 200
    saved = json.loads(config_path.read_text(encoding="utf-8"))["scheduled_plugin"]["settings"]
    assert saved["cron"] == [{"time": "22:05", "duration": 1440}, {"time": "00:00", "duration": 1}]

    before_invalid = config_path.read_bytes()
    for invalid_settings in (
        {"cron": [{"time": "25:99", "duration": 30}]},
        {"cron": [{"time": "08:30", "duration": 0}]},
        {"cron": [{"time": "08:30", "duration": 10081}]},
        {"cron": [{"time": 830, "duration": 30}]},
        {"cron": ["08:30"]},
        {"cron": [{"duration": 30}]},
    ):
        rejected = await api_plugin_settings_post(
            _FakeJsonRequest(
                {"settings": invalid_settings},
                {"plugin_name": "scheduled_plugin"},
            ),
            manager,
        )
        assert rejected.status == 400, invalid_settings
    assert config_path.read_bytes() == before_invalid


@pytest.mark.asyncio
async def test_webui_credential_urls_are_masked_and_mask_updates_preserve_storage(tmp_path):
    definition = PluginDefinition(
        name="legacy_urls",
        parameters=[
            PluginParameterDef(
                name="endpoint",
                type="str",
                default=(
                    "jdbc:postgresql://default-user:default-password@" "example.invalid/database"
                ),
            ),
            PluginParameterDef(
                name="connection",
                type="object",
                fields=[
                    {
                        "name": "callback",
                        "type": "str",
                        "default": "//default-user:default-secret@example.invalid/hook",
                    }
                ],
            ),
            PluginParameterDef(
                name="choice_endpoint",
                type="str",
                choices=[
                    "https://safe.example.invalid",
                    "jdbc:postgresql://choice-user:choice-secret@example.invalid/db",
                ],
            ),
        ],
    )
    manager = SimpleNamespace(
        data_path=tmp_path / "data",
        plugin_definitions={"legacy_urls": definition},
    )
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    config_path = plugins_dir / "_config.json"
    atomic_write_json(
        config_path,
        {
            "legacy_urls": {
                "enabled": True,
                "permissions": {},
                "settings": {
                    "endpoint": "https://stored-user:stored-password@example.invalid/api",
                    "connection": {"callback": "plans?access_token=stored-secret"},
                },
            }
        },
    )

    get_response = await api_plugin_settings_get(
        _FakeJsonRequest({}, {"plugin_name": "legacy_urls"}),
        manager,
    )
    get_payload = json.loads(get_response.text)
    masked_endpoint = _masked_parameter(definition.parameters[0])
    masked_connection = _masked_parameter(definition.parameters[1])
    masked_choice = _masked_parameter(definition.parameters[2])

    assert get_response.status == 200
    assert get_payload["settings"] == {
        "endpoint": "********",
        "connection": {"callback": "********"},
    }
    assert "default" not in masked_endpoint
    assert "default" not in masked_connection["fields"][0]
    assert masked_choice["choices"] == ["https://safe.example.invalid"]
    for secret in (
        "default-password",
        "default-secret",
        "stored-password",
        "stored-secret",
    ):
        assert secret not in get_response.text
        assert secret not in json.dumps(
            [masked_endpoint, masked_connection, masked_choice], ensure_ascii=False
        )
    assert "choice-secret" not in json.dumps(masked_choice, ensure_ascii=False)

    post_response = await api_plugin_settings_post(
        _FakeJsonRequest(
            {
                "settings": {
                    "endpoint": "********",
                    "connection": {"callback": "********"},
                }
            },
            {"plugin_name": "legacy_urls"},
        ),
        manager,
    )
    saved = json.loads(config_path.read_text(encoding="utf-8"))["legacy_urls"]["settings"]

    assert post_response.status == 200
    assert saved == {
        "endpoint": "https://stored-user:stored-password@example.invalid/api",
        "connection": {"callback": "plans?access_token=stored-secret"},
    }
    assert "stored-password" not in post_response.text
    assert "stored-secret" not in post_response.text


def test_webui_plugin_parameter_masks_sensitive_names_even_if_mislabeled():
    parameter = SimpleNamespace(
        name="api_key",
        type="str",
        description="",
        required=False,
        default="should-not-leak",
        choices=None,
        fields=[
            {"name": "repository_token", "type": "str", "default": "also-secret"},
            {"name": "label", "type": "str", "default": "visible"},
        ],
        group="",
    )

    masked = _masked_parameter(parameter)

    assert masked["default"] == "********"
    assert "default" not in masked["fields"][0]
    assert masked["fields"][1]["default"] == "visible"


def test_webui_explicit_row_identity_precedes_mutable_names_when_retaining_secrets():
    definition = PluginDefinition(
        name="identity_rows",
        parameters=[
            PluginParameterDef(
                name="accounts",
                type="object_array",
                fields=[
                    {"name": "stable_ref", "type": "str", "identity": True},
                    {"name": "id", "type": "str"},
                    {"name": "name", "type": "str"},
                    {"name": "access_token", "type": "password"},
                ],
            )
        ],
    )
    existing = {
        "accounts": [
            {
                "stable_ref": "account-a",
                "id": "old-a",
                "name": "Alpha",
                "access_token": "token-a",
            },
            {
                "stable_ref": "account-b",
                "id": "old-b",
                "name": "Beta",
                "access_token": "token-b",
            },
        ]
    }

    merged = _settings_update_without_masked_secrets(
        {
            "accounts": [
                {"stable_ref": "account-b", "id": "old-a", "name": "Alpha"},
                {"stable_ref": "account-a", "id": "old-b", "name": "Beta"},
            ]
        },
        definition,
        existing,
    )

    assert merged["accounts"] == [
        {
            "stable_ref": "account-b",
            "id": "old-a",
            "name": "Alpha",
            "access_token": "token-b",
        },
        {
            "stable_ref": "account-a",
            "id": "old-b",
            "name": "Beta",
            "access_token": "token-a",
        },
    ]


def test_webui_secret_rows_without_unambiguous_identity_reject_full_form_save():
    definition = PluginDefinition(
        name="identityless_rows",
        parameters=[
            PluginParameterDef(
                name="accounts",
                type="object_array",
                fields=[
                    {"name": "label", "type": "str"},
                    {"name": "region", "type": "str"},
                    {"name": "access_token", "type": "password"},
                ],
            )
        ],
    )
    existing = {
        "accounts": [{"label": "primary", "region": "east", "access_token": "stored-secret"}]
    }

    with pytest.raises(MaskedSecretUpdateError, match="稳定标识"):
        _settings_update_without_masked_secrets(
            {"accounts": [{"label": "primary", "region": "east"}]},
            definition,
            existing,
        )


def test_webui_object_array_credential_url_mask_follows_stable_identity():
    definition = PluginDefinition(
        name="url_rows",
        parameters=[
            PluginParameterDef(
                name="rows",
                type="object_array",
                fields=[
                    {"name": "stable_ref", "type": "str", "identity": True},
                    {"name": "endpoint", "type": "str"},
                ],
            )
        ],
    )
    existing = {
        "rows": [
            {
                "stable_ref": "primary",
                "endpoint": "https://user:password@example.invalid/api",
            }
        ]
    }

    merged = _settings_update_without_masked_secrets(
        {"rows": [{"stable_ref": "primary", "endpoint": "********"}]},
        definition,
        existing,
    )

    assert merged == existing


def test_webui_secret_retention_rejects_existing_duplicate_stable_identity():
    definition = PluginDefinition(
        name="duplicate_identity_rows",
        parameters=[
            PluginParameterDef(
                name="rows",
                type="object_array",
                fields=[
                    {"name": "stable_ref", "type": "str", "identity": True},
                    {"name": "label", "type": "str"},
                    {"name": "access_token", "type": "password"},
                ],
            )
        ],
    )
    existing = {
        "rows": [
            {"stable_ref": "same", "label": "secret", "access_token": "token"},
            {"stable_ref": "same", "label": "ordinary"},
        ]
    }

    with pytest.raises(MaskedSecretUpdateError, match="重复的稳定标识"):
        _settings_update_without_masked_secrets(
            {"rows": [{"stable_ref": "same", "label": "updated"}]},
            definition,
            existing,
        )


def test_webui_invalid_explicit_identity_never_falls_back_to_mutable_id():
    definition = PluginDefinition(
        name="invalid_identity_rows",
        parameters=[
            PluginParameterDef(
                name="rows",
                type="object_array",
                fields=[
                    {"name": "id", "type": "str"},
                    {
                        "name": "identity_blob",
                        "type": "object",
                        "identity": True,
                        "fields": [{"name": "region", "type": "str"}],
                    },
                    {"name": "access_token", "type": "password"},
                ],
            )
        ],
    )
    existing = {
        "rows": [
            {
                "id": "mutable-id",
                "identity_blob": {"region": "east"},
                "access_token": "stored-token",
            }
        ]
    }

    assert "identity" in (
        _validate_settings_schema({"rows": []}, definition, existing=existing) or ""
    )
    with pytest.raises(MaskedSecretUpdateError, match="稳定标识"):
        _settings_update_without_masked_secrets(
            {
                "rows": [
                    {
                        "id": "mutable-id",
                        "identity_blob": {"region": "west"},
                    }
                ]
            },
            definition,
            existing,
        )


def test_webui_generic_object_array_secret_uses_declared_required_identity():
    definition = PluginDefinition(
        name="generic_rows",
        parameters=[
            PluginParameterDef(
                name="accounts",
                type="object_array",
                fields=[
                    {"name": "account", "type": "str", "required": True},
                    {"name": "access_token", "type": "password"},
                ],
            )
        ],
    )
    existing = {"accounts": [{"account": "Alice", "access_token": "stored-secret"}]}

    merged = _settings_update_without_masked_secrets(
        {"accounts": [{"account": "alice"}]},
        definition,
        existing,
    )

    assert merged == {"accounts": [{"account": "alice", "access_token": "stored-secret"}]}


def test_webui_duplicate_nested_field_names_fail_closed_and_never_unmask_secret():
    definition = PluginDefinition(
        name="ambiguous_rows",
        parameters=[
            PluginParameterDef(
                name="rows",
                type="object_array",
                fields=[
                    {"name": "opaque", "type": "password"},
                    {"name": "opaque", "type": "str"},
                ],
            )
        ],
    )

    assert "重复名称" in (_validate_settings_schema({"rows": []}, definition, existing={}) or "")
    assert (
        _masked_settings(
            {"rows": [{"opaque": "must-not-leak"}]},
            definition,
        )
        == {}
    )
    assert _masked_parameter(definition.parameters[0]) == {
        "name": "",
        "type": "invalid",
        "fields": None,
    }


def test_webui_deep_nested_metadata_fails_closed_and_masks_secret_defaults():
    nested_secret = {
        "name": "level_one",
        "type": "object",
        "fields": [
            {
                "name": "level_two",
                "type": "object",
                "default": {
                    "client_secret": "default-secret",
                    "label": "visible-default",
                },
                "fields": [
                    {
                        "name": "client_secret",
                        "type": "password",
                        "default": "nested-secret",
                    },
                    {"name": "label", "type": "str", "default": "visible"},
                ],
            }
        ],
    }
    definition = PluginDefinition(
        name="nested_metadata",
        parameters=[
            PluginParameterDef(
                name="payload",
                type="object",
                fields=[nested_secret],
            )
        ],
    )

    masked = _masked_parameter(definition.parameters[0])
    level_two = masked["fields"][0]["fields"][0]

    assert level_two["default"] == {"label": "visible-default"}
    assert "default" not in level_two["fields"][0]
    assert level_two["fields"][1]["default"] == "visible"
    assert "default-secret" not in json.dumps(masked, ensure_ascii=False)
    assert "nested-secret" not in json.dumps(masked, ensure_ascii=False)

    duplicate_definition = PluginDefinition(
        name="nested_duplicate",
        parameters=[
            PluginParameterDef(
                name="payload",
                type="object",
                fields=[
                    {
                        "name": "nested",
                        "type": "object",
                        "fields": [
                            {"name": "token", "type": "password"},
                            {"name": "token", "type": "str"},
                        ],
                    }
                ],
            )
        ],
    )
    unsafe_definition = PluginDefinition(
        name="nested_unsafe",
        parameters=[
            PluginParameterDef(
                name="payload",
                type="object",
                fields=[
                    {
                        "name": "nested",
                        "type": "object",
                        "fields": [{"name": "__proto__", "type": "str"}],
                    }
                ],
            )
        ],
    )

    assert "重复名称" in (
        _validate_settings_schema({"payload": {}}, duplicate_definition, existing={}) or ""
    )
    assert "不安全名称" in (
        _validate_settings_schema({"payload": {}}, unsafe_definition, existing={}) or ""
    )
    assert "不安全字段名称" in (
        _validate_settings_schema(
            {"payload": {"nested": {"constructor": {"polluted": True}}}},
            definition,
            existing={},
        )
        or ""
    )


def test_webui_masked_object_array_secrets_follow_stable_identity_not_position():
    definition = PluginDefinition(
        name="demo",
        parameters=[
            PluginParameterDef(
                name="repos",
                type="object_array",
                fields=[
                    {"name": "owner", "type": "str"},
                    {"name": "repo", "type": "str"},
                    {"name": "repository_token", "type": "password"},
                ],
            )
        ],
    )
    existing = {
        "repos": [
            {"owner": "alpha", "repo": "one", "repository_token": "token-alpha"},
            {"owner": "beta", "repo": "two", "repository_token": "token-beta"},
        ]
    }

    removed_first = _settings_update_without_masked_secrets(
        {"repos": [{"owner": "beta", "repo": "two", "repository_token": "********"}]},
        definition,
        existing,
    )
    reordered = _settings_update_without_masked_secrets(
        {
            "repos": [
                {"owner": "beta", "repo": "two", "repository_token": "********"},
                {"owner": "alpha", "repo": "one", "repository_token": "********"},
            ]
        },
        definition,
        existing,
    )

    assert removed_first["repos"] == [
        {"owner": "beta", "repo": "two", "repository_token": "token-beta"}
    ]
    assert reordered["repos"] == [
        {"owner": "beta", "repo": "two", "repository_token": "token-beta"},
        {"owner": "alpha", "repo": "one", "repository_token": "token-alpha"},
    ]
    with pytest.raises(MaskedSecretUpdateError):
        _settings_update_without_masked_secrets(
            {
                "repos": [
                    {"owner": "changed", "repo": "one", "repository_token": "********"},
                    {"owner": "beta", "repo": "two", "repository_token": "********"},
                ]
            },
            definition,
            existing,
        )


def test_webui_repository_masks_follow_casefolded_owner_repo_identity():
    definition = PluginDefinition(
        name="repository_monitor",
        parameters=[
            PluginParameterDef(
                name="repos",
                type="object_array",
                fields=[
                    {"name": "owner", "type": "str"},
                    {"name": "repo", "type": "str"},
                    {"name": "repository_token", "type": "password"},
                ],
            )
        ],
    )
    existing = {
        "repos": [
            {"owner": "Alpha", "repo": "One", "repository_token": "alpha-token"},
            {"owner": "Beta", "repo": "Two", "repository_token": "beta-token"},
        ]
    }

    updated = _settings_update_without_masked_secrets(
        {
            "repos": [
                {"owner": "beta", "repo": "TWO", "repository_token": "********"},
                {"owner": "ALPHA", "repo": "one", "repository_token": "********"},
                # A new repository does not receive any existing credential.
                {"owner": "Gamma", "repo": "Three"},
            ]
        },
        definition,
        existing,
    )

    assert updated["repos"] == [
        {"owner": "beta", "repo": "TWO", "repository_token": "beta-token"},
        {"owner": "ALPHA", "repo": "one", "repository_token": "alpha-token"},
        {"owner": "Gamma", "repo": "Three"},
    ]


def test_webui_repository_masks_reject_new_invalid_or_duplicate_identity():
    definition = PluginDefinition(
        name="repository_monitor",
        parameters=[
            PluginParameterDef(
                name="repos",
                type="object_array",
                fields=[
                    {"name": "owner", "type": "str"},
                    {"name": "repo", "type": "str"},
                    {"name": "repository_token", "type": "password"},
                ],
            )
        ],
    )
    existing = {
        "repos": [
            {"owner": "alpha", "repo": "one", "repository_token": "alpha-token"},
            {"owner": "beta", "repo": "two", "repository_token": "beta-token"},
        ]
    }

    for rows in (
        [{"owner": "gamma", "repo": "three", "repository_token": "********"}],
        [{"owner": "bad/name", "repo": "three", "repository_token": "********"}],
        [
            {"owner": "alpha", "repo": "one", "repository_token": "********"},
            {"owner": "ALPHA", "repo": "ONE", "repository_token": "********"},
        ],
    ):
        with pytest.raises(MaskedSecretUpdateError):
            _settings_update_without_masked_secrets({"repos": rows}, definition, existing)


@pytest.mark.asyncio
async def test_webui_repository_plaintext_token_is_rejected_without_persisting(tmp_path):
    definition = PluginDefinition(
        name="repository_monitor",
        parameters=[
            PluginParameterDef(
                name="repos",
                type="object_array",
                fields=[
                    {"name": "owner", "type": "str"},
                    {"name": "repo", "type": "str"},
                    {"name": "repository_token", "type": "password"},
                ],
            )
        ],
    )
    manager = SimpleNamespace(
        data_path=tmp_path / "data", plugin_definitions={"repository_monitor": definition}
    )
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    config_path = plugins_dir / "_config.json"
    atomic_write_json(
        config_path,
        {
            "repository_monitor": {
                "enabled": True,
                "permissions": {},
                "settings": {
                    "repos": [{"owner": "alpha", "repo": "one", "repository_token": "alpha-token"}]
                },
            }
        },
    )
    before = config_path.read_bytes()

    response = await api_plugin_setting_post(
        _FakeJsonRequest(
            {"value": [{"owner": "alpha", "repo": "one", "repository_token": "new-token"}]},
            {"plugin_name": "repository_monitor", "key": "repos"},
        ),
        manager,
    )

    assert response.status == 400
    assert "new-token" not in response.text
    assert config_path.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://user:password@example.invalid/path",
        "jdbc:postgresql://dbuser:dbpass@example.invalid/database",
        "//dbuser:dbpass@example.invalid/database",
        "https://example.invalid/path?access_token=secret",
        "https://example.invalid/path?api-key=secret",
        "plans?accessToken=secret",
        "plans?credential=secret",
        "plans?credentials=secret",
        "plans?session-id=secret",
        "plans#refresh-token=secret",
        "https://user:password@[invalid/path",
        "https://[invalid/path?api_key=secret",
    ],
)
async def test_webui_plugin_settings_reject_credentials_embedded_in_urls(tmp_path, url):
    manager = SimpleNamespace(
        data_path=tmp_path / "data",
        plugin_definitions={"demo": _demo_plugin_definition()},
    )
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()

    response = await api_plugin_settings_post(
        _FakeJsonRequest(
            {"settings": {"label": url}},
            {"plugin_name": "demo"},
        ),
        manager,
    )

    assert response.status == 400
    assert "secret" not in response.text
    assert "password" not in response.text
    assert not (plugins_dir / "_config.json").exists()


@pytest.mark.asyncio
async def test_webui_plugin_settings_reject_undeclared_keys_without_persisting(tmp_path):
    manager = SimpleNamespace(
        data_path=tmp_path / "data",
        plugin_definitions={"demo": _demo_plugin_definition()},
    )
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    config_path = plugins_dir / "_config.json"
    atomic_write_json(
        config_path,
        {"demo": {"enabled": True, "permissions": {}, "settings": {"label": "old"}}},
    )

    response = await api_plugin_settings_post(
        _FakeJsonRequest(
            {"settings": {"credential": "unrecognized-secret"}},
            {"plugin_name": "demo"},
        ),
        manager,
    )

    assert response.status == 400
    assert "unrecognized-secret" not in response.text
    assert json.loads(config_path.read_text(encoding="utf-8"))["demo"]["settings"] == {
        "label": "old"
    }


@pytest.mark.asyncio
async def test_webui_malformed_recursive_metadata_fails_closed_on_reads_and_single_save(
    tmp_path,
    monkeypatch,
):
    definition = PluginDefinition(
        name="malformed_metadata",
        parameters=[
            PluginParameterDef(
                name="payload",
                type="object",
                default={"branch": {"opaque": "default-secret"}},
                fields=[
                    {
                        "name": "branch",
                        "type": "object",
                        "fields": [{"name": "opaque", "type": "str"}],
                    },
                    {
                        "name": "branch",
                        "type": "object",
                        "fields": [{"name": "opaque", "type": "password"}],
                    },
                ],
            )
        ],
    )
    manager = SimpleNamespace(
        data_path=tmp_path / "data",
        plugin_definitions={"malformed_metadata": definition},
    )
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    config_path = plugins_dir / "_config.json"
    atomic_write_json(
        config_path,
        {
            "malformed_metadata": {
                "enabled": True,
                "permissions": {},
                "settings": {"payload": {"branch": {"opaque": "stored-secret"}}},
            }
        },
    )
    before = config_path.read_bytes()
    monkeypatch.setattr(
        "sirius_pulse.webui.server_plugin_api._load_definitions_cached",
        lambda _plugins_dir: [definition],
    )

    list_response = await api_plugins_get(
        _FakeJsonRequest({}),
        manager,
    )
    detail_response = await api_plugin_detail_get(
        _FakeJsonRequest({}, {"plugin_name": "malformed_metadata"}),
        manager,
    )
    settings_response = await api_plugin_settings_get(
        _FakeJsonRequest({}, {"plugin_name": "malformed_metadata"}),
        manager,
    )
    save_response = await api_plugin_setting_post(
        _FakeJsonRequest(
            {"value": {"branch": {"opaque": "updated"}}},
            {"plugin_name": "malformed_metadata", "key": "payload"},
        ),
        manager,
    )

    assert json.loads(list_response.text)["plugins"] == []
    assert detail_response.status == 400
    assert settings_response.status == 400
    assert save_response.status == 400
    assert config_path.read_bytes() == before
    for response in (list_response, detail_response, settings_response, save_response):
        assert "stored-secret" not in response.text
        assert "default-secret" not in response.text


def test_webui_top_level_prototype_parameter_is_never_serialized():
    definition = PluginDefinition(
        name="unsafe_top_level",
        parameters=[PluginParameterDef(name="__proto__", type="str", default="secret")],
    )

    assert "不安全名称" in (_validate_settings_schema({}, definition, existing={}) or "")
    assert _masked_settings({"__proto__": "stored-secret"}, definition) == {}
    assert _masked_parameter(definition.parameters[0]) == {
        "name": "",
        "type": "invalid",
        "fields": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "path", "match_info"),
    [
        (api_plugins_get, "/api/plugins", {}),
        (api_plugin_detail_get, "/api/plugins/demo", {"plugin_name": "demo"}),
        (api_plugin_config_get, "/api/plugins/demo/config", {"plugin_name": "demo"}),
        (api_plugin_settings_get, "/api/plugins/demo/settings", {"plugin_name": "demo"}),
    ],
)
async def test_webui_plugin_reads_require_admin_before_scanning(
    tmp_path,
    monkeypatch,
    handler,
    path,
    match_info,
):
    def fail_if_scanned(*_args, **_kwargs):
        raise AssertionError("viewer requests must not scan or import plugins")

    monkeypatch.setattr(
        "sirius_pulse.webui.server_plugin_api._load_definitions_cached",
        fail_if_scanned,
    )
    request = make_mocked_request("GET", path, match_info=match_info)
    request["auth_role"] = "viewer"

    response = await handler(request, SimpleNamespace(data_path=tmp_path / "data"))

    assert response.status == 403


@pytest.mark.asyncio
async def test_webui_plugin_metadata_listing_does_not_execute_plugin_code(tmp_path):
    plugins_dir = tmp_path / "plugins"
    plugin_dir = plugins_dir / "metadata_demo"
    plugin_dir.mkdir(parents=True)
    marker = plugin_dir / "imported.txt"
    (plugin_dir / "__init__.py").write_text(
        "from pathlib import Path\n"
        "Path(__file__).with_name('imported.txt').write_text('executed')\n"
        "from sirius_pulse.plugins import PluginBase\n"
        "class MetadataDemo(PluginBase):\n"
        "    _plugin_name = 'metadata_demo'\n"
        "    _plugin_display_name = 'Metadata demo'\n"
        "    _plugin_parameters = [{'name': 'label', 'type': 'str'}]\n"
        "    @command('demo', prefix='/', patterns=['demo'], description='Demo')\n"
        "    def demo(self): pass\n",
        encoding="utf-8",
    )
    request = make_mocked_request("GET", "/api/plugins")
    request["auth_role"] = "admin"

    response = await api_plugins_get(
        request,
        SimpleNamespace(data_path=tmp_path / "data"),
    )
    payload = json.loads(response.text)

    assert response.status == 200
    assert [item["name"] for item in payload["plugins"]] == ["metadata_demo"]
    assert payload["plugins"][0]["commands"] == [
        {
            "name": "demo",
            "patterns": ["/demo"],
            "pattern_type": "prefix",
            "description": "Demo",
            "hidden_from_intent": False,
        }
    ]
    assert marker.exists() is False


@pytest.mark.asyncio
async def test_webui_plugin_ui_schema_is_serialized_but_never_persisted(tmp_path, monkeypatch):
    definition = _visual_plugin_definition()
    manager = SimpleNamespace(
        data_path=tmp_path / "data",
        plugin_definitions={"visual_demo": definition},
    )
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    monkeypatch.setattr(
        "sirius_pulse.webui.server_plugin_api._load_definitions_cached",
        lambda _plugins_dir: [definition],
    )

    list_response = await api_plugins_get(_FakeJsonRequest({}), manager)
    detail_response = await api_plugin_detail_get(
        _FakeJsonRequest({}, {"plugin_name": "visual_demo"}),
        manager,
    )
    save_response = await api_plugin_settings_post(
        _FakeJsonRequest(
            {
                "settings": {
                    "sources": [{"id": "primary", "display_name": "主站", "enabled": True}],
                    "poll_seconds": 600,
                }
            },
            {"plugin_name": "visual_demo"},
        ),
        manager,
    )

    assert list_response.status == 200
    assert detail_response.status == 200
    assert save_response.status == 200
    listed = json.loads(list_response.text)["plugins"][0]
    detail = json.loads(detail_response.text)
    assert listed["ui_schema"]["layout"] == "wide"
    assert detail["ui_schema"]["parameters"]["sources"]["item_title_field"] == "display_name"
    stored = json.loads((plugins_dir / "_config.json").read_text(encoding="utf-8"))
    assert stored["visual_demo"]["settings"] == {
        "sources": [{"id": "primary", "display_name": "主站", "enabled": True}],
        "poll_seconds": 600,
    }
    assert "ui_schema" not in stored["visual_demo"]
    assert "identity" not in json.dumps(stored, ensure_ascii=False)


@pytest.mark.asyncio
async def test_webui_mutated_parameter_contract_fails_closed_without_secret_leak(
    tmp_path,
    monkeypatch,
):
    parameter = PluginParameterDef(
        name="opaque",
        type="password",
        default="stored-secret",
    )
    definition = PluginDefinition(name="mutated_contract", parameters=[parameter])
    parameter.type = "str"
    manager = SimpleNamespace(
        data_path=tmp_path / "data",
        plugin_definitions={"mutated_contract": definition},
    )
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    monkeypatch.setattr(
        "sirius_pulse.webui.server_plugin_api._load_definitions_cached",
        lambda _plugins_dir: [definition],
    )

    list_response = await api_plugins_get(_FakeJsonRequest({}), manager)
    detail_response = await api_plugin_detail_get(
        _FakeJsonRequest({}, {"plugin_name": "mutated_contract"}),
        manager,
    )
    save_response = await api_plugin_settings_post(
        _FakeJsonRequest(
            {"settings": {"opaque": "new-secret"}},
            {"plugin_name": "mutated_contract"},
        ),
        manager,
    )

    assert json.loads(list_response.text)["plugins"] == []
    assert detail_response.status == 400
    assert save_response.status == 400
    assert not (plugins_dir / "_config.json").exists()
    for response in (list_response, detail_response, save_response):
        assert "stored-secret" not in response.text
        assert "new-secret" not in response.text


@pytest.mark.asyncio
async def test_webui_mutated_cyclic_parameter_default_fails_closed_without_recursion(
    tmp_path,
    monkeypatch,
):
    parameter = PluginParameterDef(name="payload", type="object")
    definition = PluginDefinition(name="cyclic_default", parameters=[parameter])
    cycle: list[object] = []
    cycle.append(cycle)
    parameter.default = cycle
    manager = SimpleNamespace(
        data_path=tmp_path / "data",
        plugin_definitions={"cyclic_default": definition},
    )
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    monkeypatch.setattr(
        "sirius_pulse.webui.server_plugin_api._load_definitions_cached",
        lambda _plugins_dir: [definition],
    )

    list_response = await api_plugins_get(_FakeJsonRequest({}), manager)
    detail_response = await api_plugin_detail_get(
        _FakeJsonRequest({}, {"plugin_name": "cyclic_default"}),
        manager,
    )

    assert list_response.status == 200
    assert json.loads(list_response.text)["plugins"] == []
    assert detail_response.status == 400


@pytest.mark.asyncio
async def test_webui_mutated_plugin_ui_schema_fails_closed_for_reads_and_writes(
    tmp_path,
    monkeypatch,
):
    definition = _visual_plugin_definition()
    definition.ui_schema["parameters"]["sources"]["item_title_field"] = "access_token"
    manager = SimpleNamespace(
        data_path=tmp_path / "data",
        plugin_definitions={"visual_demo": definition},
    )
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    config_path = plugins_dir / "_config.json"
    atomic_write_json(
        config_path,
        {
            "visual_demo": {
                "enabled": True,
                "permissions": {},
                "settings": {"poll_seconds": 300, "sources": []},
            }
        },
    )
    before = config_path.read_bytes()
    monkeypatch.setattr(
        "sirius_pulse.webui.server_plugin_api._load_definitions_cached",
        lambda _plugins_dir: [definition],
    )

    list_response = await api_plugins_get(_FakeJsonRequest({}), manager)
    detail_response = await api_plugin_detail_get(
        _FakeJsonRequest({}, {"plugin_name": "visual_demo"}),
        manager,
    )
    save_response = await api_plugin_settings_post(
        _FakeJsonRequest(
            {"settings": {"sources": [], "poll_seconds": 600}},
            {"plugin_name": "visual_demo"},
        ),
        manager,
    )

    listed = json.loads(list_response.text)["plugins"]
    detail = json.loads(detail_response.text)
    assert [item["name"] for item in listed] == ["visual_demo"]
    assert listed[0]["ui_schema"] == {}
    assert detail_response.status == 200
    assert detail["ui_schema"] == {}
    assert save_response.status == 200
    assert config_path.read_bytes() != before
    for response in (list_response, detail_response, save_response):
        assert "access_token" not in response.text


@pytest.mark.asyncio
async def test_webui_unknown_plugin_mutations_leave_config_untouched(tmp_path):
    manager = SimpleNamespace(
        data_path=tmp_path / "data",
        plugin_definitions={"demo": _demo_plugin_definition()},
    )
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    match_info = {"plugin_name": "missing"}

    responses = [
        await api_plugin_toggle(
            _FakeJsonRequest({"enabled": True}, match_info),
            manager,
        ),
        await api_plugin_config_post(
            _FakeJsonRequest({"developer_only": True}, match_info),
            manager,
        ),
        await api_plugin_settings_post(
            _FakeJsonRequest({"settings": {"label": "new"}}, match_info),
            manager,
        ),
        await api_plugin_setting_post(
            _FakeJsonRequest(
                {"value": "new"},
                {"plugin_name": "missing", "key": "label"},
            ),
            manager,
        ),
        await api_plugin_setting_delete(
            _FakeJsonRequest(
                {},
                {"plugin_name": "missing", "key": "label"},
            ),
            manager,
        ),
    ]

    assert all(response.status == 404 for response in responses)
    assert not (plugins_dir / "_config.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"developer_only": "false"},
        {"hidden_from_intent": 0},
        {"rate_limit_calls_per_minute": True},
        {"rate_limit_calls_per_minute": 0},
        {"rate_limit_calls_per_minute": -1},
        {"rate_limit_calls_per_minute": 1001},
    ],
)
async def test_webui_plugin_permissions_reject_non_strict_values(tmp_path, body):
    manager = SimpleNamespace(
        data_path=tmp_path / "data",
        plugin_definitions={"demo": _demo_plugin_definition()},
    )
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()

    response = await api_plugin_config_post(
        _FakeJsonRequest(body, {"plugin_name": "demo"}),
        manager,
    )

    assert response.status == 400
    assert not (plugins_dir / "_config.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", ["false", 0, 1, None])
async def test_webui_plugin_toggle_requires_exact_boolean(tmp_path, enabled):
    manager = SimpleNamespace(
        data_path=tmp_path / "data",
        plugin_definitions={"demo": _demo_plugin_definition()},
    )
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()

    response = await api_plugin_toggle(
        _FakeJsonRequest({"enabled": enabled}, {"plugin_name": "demo"}),
        manager,
    )

    assert response.status == 400
    assert not (plugins_dir / "_config.json").exists()


@pytest.mark.asyncio
async def test_webui_plugin_settings_enforce_declared_numeric_bounds(tmp_path):
    definition = PluginDefinition(
        name="monitor",
        parameters=[
            PluginParameterDef(name="poll_seconds", type="int", minimum=30, maximum=86400),
            PluginParameterDef(name="timeout", type="float", minimum=1, maximum=300),
        ],
    )
    manager = SimpleNamespace(
        data_path=tmp_path / "data",
        plugin_definitions={"monitor": definition},
    )
    (tmp_path / "plugins").mkdir()

    too_fast = await api_plugin_settings_post(
        _FakeJsonRequest({"settings": {"poll_seconds": 29}}, {"plugin_name": "monitor"}),
        manager,
    )
    non_finite = await api_plugin_settings_post(
        _FakeJsonRequest({"settings": {"timeout": float("inf")}}, {"plugin_name": "monitor"}),
        manager,
    )

    assert too_fast.status == 400
    assert non_finite.status == 400
    assert not (tmp_path / "plugins" / "_config.json").exists()


@pytest.mark.asyncio
async def test_webui_manifest_developer_only_cannot_be_relaxed(tmp_path):
    definition = PluginDefinition(
        name="locked",
        permissions=PluginPermissionDef(developer_only=True),
    )
    manager = SimpleNamespace(
        data_path=tmp_path / "data",
        plugin_definitions={"locked": definition},
    )
    (tmp_path / "plugins").mkdir()

    rejected = await api_plugin_config_post(
        _FakeJsonRequest(
            {"developer_only": False, "rate_limit_calls_per_minute": 60},
            {"plugin_name": "locked"},
        ),
        manager,
    )
    accepted = await api_plugin_config_post(
        _FakeJsonRequest(
            {"developer_only": True, "rate_limit_calls_per_minute": 60},
            {"plugin_name": "locked"},
        ),
        manager,
    )

    assert rejected.status == 400
    assert accepted.status == 200
    assert _effective_permissions(definition, {"developer_only": False})["developer_only"] is True


def test_webui_plugin_reload_when_workspace_has_personas_then_marks_each_worker(tmp_path):
    personas_dir = tmp_path / "personas"
    for name in ("alpha", "beta"):
        persona_dir = personas_dir / name
        persona_dir.mkdir(parents=True)
        (persona_dir / "persona.json").write_text("{}", encoding="utf-8")

    _request_plugin_reload(tmp_path)

    for name in ("alpha", "beta"):
        flag = personas_dir / name / "engine_state" / "reload_requested"
        assert flag.read_text(encoding="utf-8") == "all"


def test_webui_plugin_reload_when_existing_types_then_preserves_them(tmp_path):
    persona_dir = tmp_path / "personas" / "alpha"
    persona_dir.mkdir(parents=True)
    (persona_dir / "persona.json").write_text("{}", encoding="utf-8")
    flag = persona_dir / "engine_state" / "reload_requested"
    flag.parent.mkdir()
    flag.write_text('{"types": ["provider"]}', encoding="utf-8")

    _request_plugin_reload(tmp_path)

    assert set(json.loads(flag.read_text(encoding="utf-8"))["types"]) == {"all", "provider"}


@pytest.mark.asyncio
async def test_webui_plugins_get_accepts_path_based_delegate(tmp_path):
    server = WebUIServer(data_dir=tmp_path)

    response = await server.api_plugins_get(SimpleNamespace())

    assert response.status == 200
    assert json.loads(response.text) == {"plugins": []}


def test_webui_routes_when_server_is_created_then_all_declared_routes_are_registered(tmp_path):
    server = WebUIServer(data_dir=tmp_path)

    registered = _route_snapshot(server.app)

    for spec in WEBUI_ROUTES:
        assert (spec.method, spec.path) in registered
    assert not any(spec.path.startswith("/api/persona/users") for spec in WEBUI_ROUTES)
    assert not any("/api/persona/persona/interview" == spec.path for spec in WEBUI_ROUTES)
    assert ("GET", "/") in registered
    assert ("GET", "/ws/events") in registered


def test_persona_logs_when_source_is_omitted_then_reads_persona_log(tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    persona_log = logs_dir / "persona.log"
    persona_log.write_text("current runtime", encoding="utf-8")

    selected, source = _resolve_persona_log_file(tmp_path)

    assert selected == persona_log
    assert source == "persona"


@pytest.mark.asyncio
async def test_persona_logs_when_unknown_source_is_requested_then_reads_persona_log(tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    persona_log = logs_dir / "persona.log"
    persona_log.write_text("persona line", encoding="utf-8")
    request = SimpleNamespace(query={"source": "worker", "lines": "10", "offset": "0"})

    response = await api_persona_logs_get(request, tmp_path)
    payload = json.loads(response.text)

    assert payload["source"] == "persona"
    assert payload["path"] == str(persona_log)
    assert payload["lines"] == ["persona line"]


@pytest.mark.asyncio
async def test_system_logs_when_called_then_reads_global_webui_log(tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    webui_log = logs_dir / "webui.log"
    webui_log.write_text("webui line", encoding="utf-8")
    request = SimpleNamespace(query={"lines": "10", "offset": "0"})

    response = await api_system_logs_get(request, tmp_path)
    payload = json.loads(response.text)

    assert payload["target"] == "webui"
    assert payload["path"] == str(webui_log)
    assert payload["lines"] == ["webui line"]


@pytest.mark.asyncio
async def test_persona_tokens_when_cache_usage_exists_then_returns_cache_stats(tmp_path):
    store = TokenUsageStore(tmp_path / "persona.db", batch_size=1)
    store.add(
        TokenUsageRecord(
            actor_id="assistant",
            task_name="response_generate",
            model="test-model",
            prompt_tokens=100,
            completion_tokens=10,
            total_tokens=110,
            cached_prompt_tokens=75,
            uncached_prompt_tokens=25,
            cache_info_available=True,
        ),
        timestamp=1.0,
    )
    store.close()

    response = await api_persona_tokens_get(SimpleNamespace(query={}), tmp_path)
    payload = json.loads(response.text)

    assert payload["cache_stats"]["cached_prompt_tokens"] == 75
    assert payload["cache_stats"]["uncached_prompt_tokens"] == 25
    assert payload["cache_stats"]["cache_hit_rate_pct"] == 75.0
    assert payload["recent_with_breakdown"][0]["cache_info_available"] == 1


def test_webui_routes_when_declared_then_handler_names_are_available(tmp_path):
    server = WebUIServer(data_dir=tmp_path)

    for spec in WEBUI_ROUTES:
        handler = getattr(server, spec.handler_name)
        assert callable(handler)


@pytest.mark.asyncio
async def test_webui_delegated_handler_when_called_then_injects_data_dir(tmp_path, monkeypatch):
    calls = []

    async def fake_handler(request, data_dir):
        calls.append((request, data_dir))
        return web.json_response({"ok": True})

    monkeypatch.setitem(DELEGATED_HANDLERS, "api_persona_get", fake_handler)
    server = WebUIServer(data_dir=tmp_path)
    request = SimpleNamespace()

    response = await server.api_persona_get(request)

    assert response.status == 200
    assert calls == [(request, server.data_dir)]


@pytest.mark.asyncio
async def test_webui_persona_stop_when_worker_is_injected_then_shutdown_is_requested(tmp_path):
    persona_dir = tmp_path / "personas" / "sirius"
    persona_dir.mkdir(parents=True)
    atomic_write_json(tmp_path / "global_config.json", {"active_persona": "sirius"})

    class Worker:
        def __init__(self) -> None:
            self.persona_dir = persona_dir
            self.shutdown_called = False

        def shutdown(self) -> None:
            self.shutdown_called = True

    worker = Worker()
    server = WebUIServer(data_dir=tmp_path, persona_manager=worker)

    response = await server.api_persona_stop(SimpleNamespace())
    saved = json.loads((tmp_path / "global_config.json").read_text(encoding="utf-8"))

    assert response.status == 200
    assert saved["active_persona"] == ""
    assert worker.shutdown_called is True


@pytest.mark.asyncio
async def test_webui_persona_stop_when_worker_targets_other_persona_then_shutdown_is_skipped(
    tmp_path,
):
    active_dir = tmp_path / "personas" / "sirius"
    other_dir = tmp_path / "personas" / "other"
    active_dir.mkdir(parents=True)
    other_dir.mkdir(parents=True)
    atomic_write_json(tmp_path / "global_config.json", {"active_persona": "sirius"})

    class Worker:
        def __init__(self) -> None:
            self.persona_dir = other_dir
            self.shutdown_called = False

        def shutdown(self) -> None:
            self.shutdown_called = True

    worker = Worker()
    server = WebUIServer(data_dir=tmp_path, persona_manager=worker)

    response = await server.api_persona_stop(SimpleNamespace())

    assert response.status == 200
    assert worker.shutdown_called is False


@pytest.mark.asyncio
async def test_webui_persona_activate_when_called_then_updates_server_active_persona(tmp_path):
    sirius_dir = tmp_path / "personas" / "sirius"
    other_dir = tmp_path / "personas" / "other"
    sirius_dir.mkdir(parents=True)
    other_dir.mkdir(parents=True)
    atomic_write_json(sirius_dir / "persona.json", {"name": "sirius"})
    atomic_write_json(other_dir / "persona.json", {"name": "other"})
    atomic_write_json(tmp_path / "global_config.json", {"active_persona": "other"})
    server = WebUIServer(data_dir=tmp_path)
    request = SimpleNamespace(match_info={"name": "sirius"})

    response = await server.api_persona_activate(request)

    assert response.status == 200
    assert server.persona_dir == sirius_dir


@pytest.mark.asyncio
async def test_persona_status_when_worker_pid_is_stale_then_not_running(tmp_path, monkeypatch):
    persona_dir = tmp_path / "personas" / "sirius"
    persona_dir.mkdir(parents=True)
    atomic_write_json(tmp_path / "global_config.json", {"active_persona": "sirius"})
    atomic_write_json(
        persona_dir / "engine_state" / "worker_status.json",
        {"status": "running", "pid": 12345},
    )
    monkeypatch.setattr(persona_manager, "_pid_exists", lambda pid: False)

    response = await persona_manager.api_persona_status(SimpleNamespace(), persona_dir)
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["running"] is False
    assert payload["pid"] == 12345


@pytest.mark.asyncio
async def test_persona_start_when_not_running_then_spawns_worker_process(tmp_path, monkeypatch):
    persona_dir = tmp_path / "personas" / "sirius"
    persona_dir.mkdir(parents=True)
    atomic_write_json(persona_dir / "persona.json", {"name": "sirius"})
    atomic_write_json(tmp_path / "global_config.json", {"active_persona": "", "log_level": "debug"})
    monkeypatch.setattr(persona_manager, "_pid_exists", lambda pid: False)
    calls = []

    class FakeProcess:
        pid = 24680

    def fake_popen(command, stdout, stderr, **kwargs):
        calls.append({"command": command, "stdout": stdout, "stderr": stderr, "kwargs": kwargs})
        return FakeProcess()

    monkeypatch.setattr(persona_manager.subprocess, "Popen", fake_popen)

    response = await persona_manager.api_persona_start(SimpleNamespace(), persona_dir)
    payload = json.loads(response.text)
    saved = json.loads((tmp_path / "global_config.json").read_text(encoding="utf-8"))

    assert response.status == 200
    assert payload["success"] is True
    assert payload["started"] is True
    assert payload["pid"] == 24680
    assert saved["active_persona"] == "sirius"
    assert calls[0]["command"][1:5] == [
        "-m",
        "sirius_pulse.persona_worker",
        "--config",
        str(persona_dir),
    ]
    assert calls[0]["command"][-2:] == ["--log-level", "DEBUG"]
    assert calls[0]["kwargs"]["cwd"] == str(tmp_path.parent)


@pytest.mark.asyncio
async def test_persona_start_when_previous_spawn_is_starting_then_does_not_spawn_again(
    tmp_path, monkeypatch
):
    persona_dir = tmp_path / "personas" / "sirius"
    persona_dir.mkdir(parents=True)
    atomic_write_json(persona_dir / "persona.json", {"name": "sirius"})
    atomic_write_json(tmp_path / "global_config.json", {"active_persona": "sirius"})
    atomic_write_json(
        persona_dir / "engine_state" / "worker_status.json",
        {"status": "starting", "pid": 24680, "started_at": "2026-07-07T00:00:00+00:00"},
    )
    monkeypatch.setattr(persona_manager, "_pid_exists", lambda pid: True)

    def fake_popen(*args, **kwargs):
        raise AssertionError("should not spawn another worker")

    monkeypatch.setattr(persona_manager.subprocess, "Popen", fake_popen)

    response = await persona_manager.api_persona_start(SimpleNamespace(), persona_dir)
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["success"] is True
    assert payload["started"] is False
    assert payload["already_running"] is True
    assert payload["pid"] == 24680


@pytest.mark.asyncio
async def test_persona_start_when_two_personas_are_requested_then_spawns_both_workers(
    tmp_path, monkeypatch
):
    first_dir = tmp_path / "personas" / "first"
    second_dir = tmp_path / "personas" / "second"
    for persona_dir in (first_dir, second_dir):
        persona_dir.mkdir(parents=True)
        atomic_write_json(persona_dir / "persona.json", {"name": persona_dir.name})
    atomic_write_json(tmp_path / "global_config.json", {"active_persona": ""})
    monkeypatch.setattr(persona_manager, "_pid_exists", lambda pid: False)
    calls = []

    class FakeProcess:
        def __init__(self, pid):
            self.pid = pid

    def fake_popen(command, stdout, stderr, **kwargs):
        calls.append(command)
        return FakeProcess(30000 + len(calls))

    monkeypatch.setattr(persona_manager.subprocess, "Popen", fake_popen)

    first_response = await persona_manager.api_persona_start(SimpleNamespace(), first_dir)
    second_response = await persona_manager.api_persona_start(SimpleNamespace(), second_dir)

    assert json.loads(first_response.text)["started"] is True
    assert json.loads(second_response.text)["started"] is True
    assert len(calls) == 2
    assert str(first_dir) in calls[0]
    assert str(second_dir) in calls[1]
    assert (
        json.loads((first_dir / "engine_state" / "worker_status.json").read_text())["pid"] == 30001
    )
    assert (
        json.loads((second_dir / "engine_state" / "worker_status.json").read_text())["pid"] == 30002
    )
    config = json.loads((tmp_path / "global_config.json").read_text())
    assert config["active_personas"] == ["first", "second"]
    assert config["active_persona"] == "first"


@pytest.mark.asyncio
async def test_webui_named_persona_start_targets_requested_persona(tmp_path, monkeypatch):
    first_dir = tmp_path / "personas" / "first"
    second_dir = tmp_path / "personas" / "second"
    for persona_dir in (first_dir, second_dir):
        persona_dir.mkdir(parents=True)
        atomic_write_json(persona_dir / "persona.json", {"name": persona_dir.name})
    atomic_write_json(tmp_path / "global_config.json", {"active_persona": "first"})
    monkeypatch.setattr(persona_manager, "_pid_exists", lambda pid: False)
    calls = []

    class FakeProcess:
        pid = 24681

    def fake_popen(command, stdout, stderr, **kwargs):
        calls.append(command)
        return FakeProcess()

    monkeypatch.setattr(persona_manager.subprocess, "Popen", fake_popen)
    server = WebUIServer(data_dir=tmp_path)
    request = SimpleNamespace(match_info={"name": "second"})

    response = await server.api_persona_named_start(request)
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["active"] == "second"
    assert str(second_dir) in calls[0]
    assert not (first_dir / "engine_state" / "worker_status.json").exists()


def test_webui_shutdown_persona_manager_targets_the_matching_worker(tmp_path):
    first_dir = tmp_path / "personas" / "first"
    second_dir = tmp_path / "personas" / "second"

    class Worker:
        def __init__(self, persona_dir):
            self.persona_dir = persona_dir
            self.shutdown_calls = 0

        def shutdown(self):
            self.shutdown_calls += 1

    first = Worker(first_dir)
    second = Worker(second_dir)
    server = WebUIServer(data_dir=tmp_path, persona_manager={"first": first, "second": second})

    assert server._shutdown_persona_manager(second_dir)
    assert second.shutdown_calls == 1
    assert first.shutdown_calls == 0


@pytest.mark.asyncio
async def test_webui_monitoring_overview_includes_all_personas(tmp_path):
    for name, pid in (("first", 31001), ("second", 31002)):
        persona_dir = tmp_path / "personas" / name
        persona_dir.mkdir(parents=True)
        atomic_write_json(persona_dir / "persona.json", {"name": name})
        atomic_write_json(
            persona_dir / "engine_state" / "worker_status.json",
            {"status": "running", "pid": pid},
        )

    server = WebUIServer(data_dir=tmp_path)
    response = await server.api_monitoring_overview(SimpleNamespace())
    payload = json.loads(response.text)

    assert payload["total_personas"] == 2
    assert payload["running_personas"] == 2
    assert {item["name"] for item in payload["personas"]} == {"first", "second"}


@pytest.mark.asyncio
async def test_persona_stop_when_external_worker_is_running_then_sends_sigterm(
    tmp_path, monkeypatch
):
    persona_dir = tmp_path / "personas" / "sirius"
    persona_dir.mkdir(parents=True)
    atomic_write_json(tmp_path / "global_config.json", {"active_persona": "sirius"})
    atomic_write_json(
        persona_dir / "engine_state" / "worker_status.json",
        {"status": "running", "pid": 24680},
    )
    kills = []

    monkeypatch.setattr(persona_manager, "_pid_exists", lambda pid: len(kills) == 0)
    monkeypatch.setattr(persona_manager.os, "kill", lambda pid, sig: kills.append((pid, sig)))

    response = await persona_manager.api_persona_stop(SimpleNamespace(), persona_dir)
    payload = json.loads(response.text)
    saved = json.loads((tmp_path / "global_config.json").read_text(encoding="utf-8"))

    assert response.status == 200
    assert payload["success"] is True
    assert payload["stopped"] is True
    assert saved["active_persona"] == ""
    assert kills == [(24680, persona_manager.signal.SIGTERM)]


# ─── 全局配置（AMKR 连接） ────────────────────────────────


def _empty_request():
    return make_mocked_request("GET", "/api/global-config")


@pytest.mark.asyncio
async def test_global_config_post_when_amkr_fields_sent_then_persisted(tmp_path):
    """AMKR 连接配置由 WebUI 维护，需能落盘并被 runtime 读到。"""
    server = WebUIServer(data_dir=tmp_path)

    response = await server.api_global_config_post(
        _FakeJsonRequest(
            {
                "amkr_base_url": "http://amkr.internal:8000/",
                "amkr_local_api_key": "sk-amkr-admin",
                "amkr_workspace": "sirius-pulse",
                "amkr_ui_enabled": False,
            }
        )
    )
    saved = json.loads((tmp_path / "global_config.json").read_text(encoding="utf-8"))

    assert response.status == 200
    assert saved["amkr_base_url"] == "http://amkr.internal:8000/"
    assert saved["amkr_local_api_key"] == "sk-amkr-admin"
    assert saved["amkr_workspace"] == "sirius-pulse"
    assert saved["amkr_ui_enabled"] is False


@pytest.mark.asyncio
async def test_global_config_post_when_public_url_sent_then_persisted(tmp_path):
    """容器走回环、浏览器走反代域名时，两个地址都必须能被运维保存下来。"""
    server = WebUIServer(data_dir=tmp_path)

    response = await server.api_global_config_post(
        _FakeJsonRequest(
            {
                "amkr_base_url": "http://127.0.0.1:28881",
                "amkr_public_url": "https://amkr.example.com",
            }
        )
    )
    saved = json.loads((tmp_path / "global_config.json").read_text(encoding="utf-8"))

    assert response.status == 200
    assert saved["amkr_base_url"] == "http://127.0.0.1:28881"
    assert saved["amkr_public_url"] == "https://amkr.example.com"


@pytest.mark.asyncio
async def test_global_config_get_when_key_set_then_returns_mask(tmp_path):
    """管理员 Key 不能以明文回显给前端。"""
    atomic_write_json(
        tmp_path / "global_config.json",
        {"amkr_local_api_key": "sk-amkr-admin-secret", "amkr_workspace": "sirius-pulse"},
    )
    server = WebUIServer(data_dir=tmp_path)

    response = await server.api_global_config_get(_empty_request())
    payload = json.loads(response.text)

    assert payload["amkr_local_api_key"] == "sk-a****"
    assert "secret" not in payload["amkr_local_api_key"]
    assert payload["amkr_workspace"] == "sirius-pulse"


@pytest.mark.asyncio
async def test_global_config_get_when_panel_keys_stored_then_never_echoed(tmp_path):
    """面板 key 映射绝不能随全局配置回显。

    这个接口任何已登录用户（含只读角色）都能读，而面板 key 能改那个空间的任务；
    它只从管理员专用的 /api/amkr/panel 取。
    """
    atomic_write_json(
        tmp_path / "global_config.json",
        {
            "amkr_local_api_key": "sk-amkr-admin-secret",
            "amkr_panel_keys": {"sp/sirius": "amkr_ws_secret"},
        },
    )
    server = WebUIServer(data_dir=tmp_path)

    response = await server.api_global_config_get(_empty_request())

    assert "amkr_ws_secret" not in response.text
    assert "amkr_panel_keys" not in json.loads(response.text)


@pytest.mark.asyncio
async def test_global_config_post_when_masked_key_echoed_then_keeps_stored_secret(tmp_path):
    """前端回显掩码时不得把掩码写回磁盘，否则 Key 会被悄悄破坏。"""
    atomic_write_json(tmp_path / "global_config.json", {"amkr_local_api_key": "sk-real-secret"})
    server = WebUIServer(data_dir=tmp_path)

    response = await server.api_global_config_post(
        _FakeJsonRequest({"amkr_local_api_key": "sk-r****", "amkr_workspace": "other"})
    )
    saved = json.loads((tmp_path / "global_config.json").read_text(encoding="utf-8"))

    assert response.status == 200
    assert saved["amkr_local_api_key"] == "sk-real-secret"
    assert saved["amkr_workspace"] == "other"


@pytest.mark.asyncio
async def test_global_config_post_when_key_empty_then_keeps_stored_secret(tmp_path):
    """空串表示「不改动」，避免误清空凭据。"""
    atomic_write_json(tmp_path / "global_config.json", {"amkr_local_api_key": "sk-real-secret"})
    server = WebUIServer(data_dir=tmp_path)

    await server.api_global_config_post(_FakeJsonRequest({"amkr_local_api_key": ""}))
    saved = json.loads((tmp_path / "global_config.json").read_text(encoding="utf-8"))

    assert saved["amkr_local_api_key"] == "sk-real-secret"


# ─── AMKR 运维页 ──────────────────────────────────────────


def _refuse_all_amkr_connections(monkeypatch) -> None:
    """让 AMKR 探测立即失败，避免测试真的去解析域名或连端口。"""

    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda *a, **k: real_client(transport=httpx.MockTransport(handler)),
    )


def test_webui_routes_when_registered_then_amkr_ops_routes_exist_and_orchestration_is_gone():
    """编排接口随编排页一并下线，替换为只读巡检、面板地址、任务登记与凭据轮换。"""
    registered = {(spec.method, spec.path) for spec in WEBUI_ROUTES}

    assert ("GET", "/api/amkr/status") in registered
    assert ("GET", "/api/amkr/panel") in registered
    assert ("POST", "/api/amkr/register") in registered
    assert ("POST", "/api/amkr/rotate-inference-key") in registered
    assert not any("/orchestration" in path for _, path in registered)
    assert not any("/task-params" in path for _, path in registered)


def test_webui_routes_when_registered_then_embedding_rebuild_replaces_restart():
    """本地 embedding 服务已下线，接口从「重启服务」改为「重建索引」。"""
    registered = {(spec.method, spec.path) for spec in WEBUI_ROUTES}

    assert ("GET", "/api/embedding/status") in registered
    assert ("POST", "/api/embedding/rebuild") in registered
    assert ("POST", "/api/embedding/restart") not in registered


@pytest.mark.asyncio
async def test_embedding_rebuild_when_rebuilt_then_reports_counts_and_wakes_worker(
    tmp_path, monkeypatch
):
    """重建要覆盖记忆单元，并唤醒人格进程丢弃旧向量缓存。"""
    (tmp_path / "personas" / "sirius").mkdir(parents=True)
    server = WebUIServer(data_dir=tmp_path)
    monkeypatch.setattr(server, "_rebuild_memory_unit_embeddings", lambda: (3, 0))
    reloads: list[str] = []
    monkeypatch.setattr(server, "_notify_config_reload", reloads.append)

    response = await server.api_embedding_rebuild(
        make_mocked_request("POST", "/api/embedding/rebuild")
    )
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload == {"success": True, "units": 3, "failed": 0}
    assert reloads == ["memory"]


@pytest.mark.asyncio
async def test_embedding_rebuild_when_some_vectors_fail_then_reports_failure(tmp_path, monkeypatch):
    """部分向量重算失败必须报失败。

    线上确实出现过两个批次超时：接口原本照样返回 success，用户以为索引已修好，实际
    还有 128 条留在旧维度、被判为不相似。这里锁住「有失败就不能报成功」。
    """
    (tmp_path / "personas" / "sirius").mkdir(parents=True)
    server = WebUIServer(data_dir=tmp_path)
    monkeypatch.setattr(server, "_rebuild_memory_unit_embeddings", lambda: (9026, 128))

    response = await server.api_embedding_rebuild(
        make_mocked_request("POST", "/api/embedding/rebuild")
    )
    payload = json.loads(response.text)

    assert payload["success"] is False
    assert payload["failed"] == 128
    assert payload["units"] == 9026
    assert "128" in payload["error"]


@pytest.mark.asyncio
async def test_embedding_rebuild_when_nothing_indexed_then_skips_worker_wakeup(
    tmp_path, monkeypatch
):
    """没有任何索引时可重建内容为空，不必打扰人格进程。"""
    (tmp_path / "personas" / "sirius").mkdir(parents=True)
    server = WebUIServer(data_dir=tmp_path)
    monkeypatch.setattr(server, "_rebuild_memory_unit_embeddings", lambda: (0, 0))
    reloads: list[str] = []
    monkeypatch.setattr(server, "_notify_config_reload", reloads.append)

    response = await server.api_embedding_rebuild(
        make_mocked_request("POST", "/api/embedding/rebuild")
    )

    assert json.loads(response.text)["success"] is True
    assert reloads == []


@pytest.mark.asyncio
async def test_embedding_rebuild_when_rebuild_fails_then_reports_error(tmp_path, monkeypatch):
    """重建失败要如实报错，不能假装成功让用户以为索引已经可用。"""
    (tmp_path / "personas" / "sirius").mkdir(parents=True)
    server = WebUIServer(data_dir=tmp_path)

    def boom() -> int:
        raise RuntimeError("AMKR 不可达")

    monkeypatch.setattr(server, "_rebuild_memory_unit_embeddings", boom)

    response = await server.api_embedding_rebuild(
        make_mocked_request("POST", "/api/embedding/rebuild")
    )
    payload = json.loads(response.text)

    assert payload["success"] is False
    assert "AMKR 不可达" in payload["error"]


@pytest.mark.asyncio
async def test_amkr_status_get_when_key_missing_then_asks_for_configuration(tmp_path):
    """未配置凭据时页面要给出可操作提示，而不是空白或 500。"""
    server = WebUIServer(data_dir=tmp_path)

    response = await server.api_amkr_status_get(_empty_request())
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["configured"] is False
    assert "amkr_local_api_key" in payload["error"]
    assert payload["known_tasks"]


@pytest.mark.asyncio
async def test_amkr_status_get_when_configured_then_reports_personas_and_ui_url(
    tmp_path, monkeypatch
):
    """运维页需要看到连的是哪个 AMKR、以及每个人格的子空间名。"""
    _refuse_all_amkr_connections(monkeypatch)
    atomic_write_json(
        tmp_path / "global_config.json",
        {"amkr_base_url": "http://amkr.test", "amkr_local_api_key": "sk-x", "amkr_workspace": "sp"},
    )
    (tmp_path / "personas" / "sirius").mkdir(parents=True)
    atomic_write_json(tmp_path / "personas" / "sirius" / "persona.json", {"name": "月白"})
    server = WebUIServer(data_dir=tmp_path)

    response = await server.api_amkr_status_get(_empty_request())
    payload = json.loads(response.text)

    assert payload["base_url"] == "http://amkr.test"
    assert payload["ui_url"] == "http://amkr.test/ui"
    assert payload["workspace_base"] == "sp"
    assert payload["reachable"] is False
    assert "无法连接 AMKR" in payload["error"]


@pytest.mark.asyncio
async def test_amkr_register_post_when_no_personas_then_rejects_with_message(tmp_path):
    """没有人格时无处可注册，应直接拒绝而不是静默成功。"""
    server = WebUIServer(data_dir=tmp_path)

    response = await server.api_amkr_register_post(_FakeJsonRequest({}))

    assert response.status == 400
    assert "人格" in json.loads(response.text)["error"]


@pytest.mark.asyncio
async def test_amkr_register_post_when_key_missing_then_reports_per_persona_error(tmp_path):
    """凭据缺失时逐人格报错，前端能指出是哪个人的任务没登记上。"""
    (tmp_path / "personas" / "sirius").mkdir(parents=True)
    atomic_write_json(tmp_path / "personas" / "sirius" / "persona.json", {"name": "月白"})
    server = WebUIServer(data_dir=tmp_path)

    response = await server.api_amkr_register_post(_FakeJsonRequest({}))
    payload = json.loads(response.text)

    assert response.status == 200
    assert "amkr_local_api_key" in payload["results"]["sirius"]["error"]


@pytest.mark.asyncio
async def test_amkr_register_post_when_persona_given_then_only_that_persona_is_touched(
    tmp_path, monkeypatch
):
    """单个工作空间的「注册缺失项」按钮不该顺手改动其他人格的空间。"""
    _refuse_all_amkr_connections(monkeypatch)
    for name in ("sirius", "alice"):
        (tmp_path / "personas" / name).mkdir(parents=True)
        atomic_write_json(tmp_path / "personas" / name / "persona.json", {"name": name})
    server = WebUIServer(data_dir=tmp_path)

    response = await server.api_amkr_register_post(_FakeJsonRequest({"persona": "alice"}))
    payload = json.loads(response.text)

    assert response.status == 200
    assert list(payload["results"]) == ["alice"]


# ─── 工作空间面板（管理员专用） ────────────────────────────


def _panel_request(role: str, persona: str = "sirius"):
    """造一个带角色与 persona 查询参数的 GET 请求。"""
    query = f"?persona={persona}" if persona else ""
    request = make_mocked_request("GET", f"/api/amkr/panel{query}")
    request["auth_role"] = role
    return request


def _panel_persona(tmp_path, name: str = "sirius") -> Path:
    persona_dir = tmp_path / "personas" / name
    persona_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(persona_dir / "persona.json", {"name": name})
    return persona_dir


@pytest.mark.asyncio
async def test_amkr_panel_get_when_viewer_then_denied(tmp_path):
    """面板地址里是明文凭据：只读角色不能取，否则 viewer 就能改那个空间的任务。"""
    _panel_persona(tmp_path)
    server = WebUIServer(data_dir=tmp_path)

    response = await server.api_amkr_panel_get(_panel_request("viewer"))

    assert response.status == 403
    assert "管理员" in json.loads(response.text)["error"]


@pytest.mark.asyncio
async def test_amkr_panel_get_when_admin_then_returns_embed_url_with_key_in_fragment(
    tmp_path, monkeypatch
):
    """管理员拿到可嵌入地址；凭据必须在 fragment 里，不能落到查询串。"""
    _refuse_all_amkr_connections(monkeypatch)
    _panel_persona(tmp_path)
    atomic_write_json(
        tmp_path / "global_config.json",
        {
            "amkr_base_url": "http://amkr.test",
            "amkr_local_api_key": "sk-x",
            "amkr_workspace": "sp",
            "amkr_panel_keys": {"sp/sirius": "amkr_ws_secret"},
        },
    )
    server = WebUIServer(data_dir=tmp_path)

    response = await server.api_amkr_panel_get(_panel_request("admin"))
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["url"] == "http://amkr.test/ui/panel.html#k=amkr_ws_secret"
    assert "?" not in payload["url"]


@pytest.mark.asyncio
async def test_amkr_panel_get_when_unknown_persona_then_404(tmp_path):
    """不存在的人格应被拒，避免拿它去拼一个注定 401 的地址。"""
    _panel_persona(tmp_path)
    server = WebUIServer(data_dir=tmp_path)

    response = await server.api_amkr_panel_get(_panel_request("admin", persona="nobody"))

    assert response.status == 404


@pytest.mark.asyncio
async def test_amkr_panel_get_when_no_key_stored_then_explains_how_to_recover(tmp_path):
    """没有 key 时给出可操作的补救指引，而不是一个加载后 401 的面板地址。"""
    _panel_persona(tmp_path)
    server = WebUIServer(data_dir=tmp_path)

    response = await server.api_amkr_panel_get(_panel_request("admin"))
    payload = json.loads(response.text)

    assert response.status == 409
    assert "api_key" in payload["error"]


@pytest.mark.asyncio
async def test_amkr_status_get_when_key_stored_then_does_not_leak_it(tmp_path, monkeypatch):
    """只读状态接口必须能说明「面板可用」，但绝不能带出 key 本身。"""
    _refuse_all_amkr_connections(monkeypatch)
    _panel_persona(tmp_path)
    atomic_write_json(
        tmp_path / "global_config.json",
        {
            "amkr_base_url": "http://amkr.test",
            "amkr_local_api_key": "sk-x",
            "amkr_workspace": "sp",
            "amkr_panel_keys": {"sp/sirius": "amkr_ws_secret"},
        },
    )
    server = WebUIServer(data_dir=tmp_path)

    response = await server.api_amkr_status_get(_empty_request())
    payload = json.loads(response.text)

    assert payload["reachable"] is False
    assert payload["workspaces"][0]["panel_ready"] is True


# ─── 推理 key 轮换 ─────────────────────────────────────────


class _RoleJsonRequest(_FakeJsonRequest):
    """带 ``auth_role`` 的 JSON 请求替身（轮换接口要同时读角色与请求体）。"""

    def __init__(self, payload: dict[str, object], role: str) -> None:
        super().__init__(payload)
        self._role = role

    def get(self, key: str, default: object = None) -> object:
        return self._role if key == "auth_role" else default


def _rotate_request(role: str, persona: str = "sirius"):
    """造一个带角色与 persona 的 POST 请求。"""
    return _RoleJsonRequest({"persona": persona}, role)


def _accept_rotation(monkeypatch, *, new_key="amkr_ik_rotated") -> None:
    """让 AMKR 接受轮换请求并返回一把新 key。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"name": "sp/sirius", "inference_key": new_key, "config_revision": "r"}
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda *a, **k: real_client(transport=httpx.MockTransport(handler)),
    )


def _server_with_amkr(tmp_path) -> WebUIServer:
    atomic_write_json(
        tmp_path / "global_config.json",
        {
            "amkr_base_url": "http://amkr.test",
            "amkr_local_api_key": "sk-x",
            "amkr_workspace": "sp",
        },
    )
    return WebUIServer(data_dir=tmp_path)


@pytest.mark.asyncio
async def test_amkr_rotate_inference_key_when_admin_then_returns_and_stores_new_key(
    tmp_path, monkeypatch
):
    """轮换要同时做两件事：回给运维看，并落到本地供 provider 取用。

    只回不存的话引擎下次构建仍拿旧 key（已失效）；只存不回则运维没有补救手段。
    """
    _panel_persona(tmp_path)
    _accept_rotation(monkeypatch, new_key="amkr_ik_fresh")
    server = _server_with_amkr(tmp_path)

    response = await server.api_amkr_rotate_inference_key_post(_rotate_request("admin"))
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["inference_key"] == "amkr_ik_fresh"
    saved = json.loads((tmp_path / "global_config.json").read_text(encoding="utf-8"))
    assert saved["amkr_inference_keys"] == {"sp/sirius": "amkr_ik_fresh"}


@pytest.mark.asyncio
async def test_amkr_rotate_inference_key_when_viewer_then_denied(tmp_path, monkeypatch):
    """轮换会作废正在用的凭据：只读角色不能触发。"""
    _panel_persona(tmp_path)
    _accept_rotation(monkeypatch)
    server = _server_with_amkr(tmp_path)

    response = await server.api_amkr_rotate_inference_key_post(_rotate_request("viewer"))

    assert response.status == 403
    assert "管理员" in json.loads(response.text)["error"]


@pytest.mark.asyncio
async def test_amkr_rotate_inference_key_when_unknown_persona_then_404(tmp_path, monkeypatch):
    """不存在的人格不该被拿去轮换，否则会在 AMKR 侧对一个陌生空间动手。"""
    _panel_persona(tmp_path)
    _accept_rotation(monkeypatch)
    server = _server_with_amkr(tmp_path)

    response = await server.api_amkr_rotate_inference_key_post(
        _rotate_request("admin", persona="nobody")
    )

    assert response.status == 404


@pytest.mark.asyncio
async def test_amkr_rotate_inference_key_when_no_persona_then_400(tmp_path, monkeypatch):
    """缺 persona 时明确报错，而不是挑一个人格默默轮换掉。"""
    _panel_persona(tmp_path)
    _accept_rotation(monkeypatch)
    server = _server_with_amkr(tmp_path)

    response = await server.api_amkr_rotate_inference_key_post(_rotate_request("admin", persona=""))

    assert response.status == 400


@pytest.mark.asyncio
async def test_amkr_rotate_inference_key_when_amkr_unreachable_then_reports_502(
    tmp_path, monkeypatch
):
    """AMKR 侧失败要如实回报，且不能改动本地已存的凭据。"""
    _panel_persona(tmp_path)
    _refuse_all_amkr_connections(monkeypatch)
    atomic_write_json(
        tmp_path / "global_config.json",
        {
            "amkr_base_url": "http://amkr.test",
            "amkr_local_api_key": "sk-x",
            "amkr_workspace": "sp",
            "amkr_inference_keys": {"sp/sirius": "amkr_ik_existing"},
        },
    )
    server = WebUIServer(data_dir=tmp_path)

    response = await server.api_amkr_rotate_inference_key_post(_rotate_request("admin"))

    assert response.status == 502
    saved = json.loads((tmp_path / "global_config.json").read_text(encoding="utf-8"))
    assert saved["amkr_inference_keys"] == {"sp/sirius": "amkr_ik_existing"}


@pytest.mark.asyncio
async def test_amkr_rotate_inference_key_when_other_persona_active_then_notifies_the_rotated_one(
    tmp_path, monkeypatch
):
    """轮换必须通知**被轮换**的人格，而不是当时活跃的那个。

    凭据是按人格分开的，只有一个真正换了。通知错对象的话，活跃人格白重建一次，而
    真正换了 key 的那个人格继续拿旧 key 跑——它要等到下一次对话才以 401 暴露。
    """
    _panel_persona(tmp_path, "sirius")
    _panel_persona(tmp_path, "other")
    _accept_rotation(monkeypatch, new_key="amkr_ik_fresh")
    server = _server_with_amkr(tmp_path)
    # 活跃人格是 sirius，但我们轮换的是 other。
    server._active_persona_name = "sirius"

    response = await server.api_amkr_rotate_inference_key_post(
        _rotate_request("admin", persona="other")
    )

    assert response.status == 200
    flag = tmp_path / "personas" / "other" / "engine_state" / "reload_requested"
    assert flag.exists(), "被轮换的人格必须收到 provider 重载标志"
    assert "provider" in flag.read_text(encoding="utf-8")
    active_flag = tmp_path / "personas" / "sirius" / "engine_state" / "reload_requested"
    assert not active_flag.exists(), "不该去动活跃人格"


@pytest.mark.asyncio
async def test_global_config_get_when_inference_keys_stored_then_never_echoed(tmp_path):
    """推理 key 映射与面板 key 同等对待：整个字段都不回显。

    这个接口任何已登录角色都能读，回显等于把每个空间的推理凭据发给所有用户。
    """
    atomic_write_json(
        tmp_path / "global_config.json",
        {
            "amkr_local_api_key": "sk-x",
            "amkr_inference_keys": {"sp/sirius": "amkr_ik_secret"},
        },
    )
    server = WebUIServer(data_dir=tmp_path)

    response = await server.api_global_config_get(_empty_request())
    payload = json.loads(response.text)

    assert "amkr_inference_keys" not in payload
    assert "amkr_ik_secret" not in response.text


@pytest.mark.asyncio
async def test_global_config_post_when_inference_keys_present_then_not_overwritten(tmp_path):
    """全局配置保存不能顺手把推理 key 抹掉：前端表单里根本没有这个字段。

    一旦被覆盖，每个人格都会在下次构建时缺凭据而起不来。
    """
    atomic_write_json(
        tmp_path / "global_config.json",
        {
            "amkr_local_api_key": "sk-x",
            "amkr_inference_keys": {"sp/sirius": "amkr_ik_secret"},
        },
    )
    server = WebUIServer(data_dir=tmp_path)

    await server.api_global_config_post(_FakeJsonRequest({"amkr_workspace": "other"}))

    saved = json.loads((tmp_path / "global_config.json").read_text(encoding="utf-8"))
    assert saved["amkr_inference_keys"] == {"sp/sirius": "amkr_ik_secret"}


@pytest.mark.asyncio
async def test_persona_post_when_aliases_is_a_string_then_rejects_instead_of_shredding(
    tmp_path: Path,
):
    """aliases 传字符串必须 400。

    直接 setattr + to_dict 的 list() 会把 "月白" 变成 ["月","白"]——两次单字别名，
    用户看到的是"保存成功"，实际人设已经被改坏。
    """
    from sirius_pulse.webui.persona_api import api_persona_post

    persona_dir = tmp_path / "personas" / "sirius"
    persona_dir.mkdir(parents=True)

    response = await api_persona_post(
        _FakeJsonRequest({"aliases": "月白", "name": "月白"}), persona_dir
    )

    assert response.status == 400
    assert not (persona_dir / "persona.json").exists()


@pytest.mark.asyncio
async def test_persona_post_when_name_is_not_a_string_then_rejects(tmp_path: Path):
    from sirius_pulse.webui.persona_api import api_persona_post

    persona_dir = tmp_path / "personas" / "sirius"
    persona_dir.mkdir(parents=True)

    response = await api_persona_post(_FakeJsonRequest({"name": ["月", "白"]}), persona_dir)

    assert response.status == 400


@pytest.mark.asyncio
async def test_persona_post_when_payload_is_valid_then_persists(tmp_path: Path):
    from sirius_pulse.webui.persona_api import api_persona_post
    from sirius_pulse.core.persona_store import PersonaStore

    persona_dir = tmp_path / "personas" / "sirius"
    persona_dir.mkdir(parents=True)

    response = await api_persona_post(
        _FakeJsonRequest(
            {"name": "月白", "aliases": ["Sirius"], "full_system_prompt": "你是月白。"}
        ),
        persona_dir,
    )

    assert response.status == 200
    saved = PersonaStore.load(persona_dir)
    assert saved is not None
    assert saved.name == "月白"
    assert saved.aliases == ["Sirius"]


@pytest.mark.asyncio
async def test_adapters_post_when_body_has_unknown_keys_then_it_does_not_500(tmp_path: Path):
    """带未知键的请求不能让适配器接口 500。

    旧版页面或手写请求会多带字段，``AdapterConfig(**a)`` 遇到未知键直接 TypeError，
    接口变成 500，而用户只是想保存一个连接配置。
    """
    from sirius_pulse.webui.persona_api import api_adapters_post

    persona_dir = tmp_path / "personas" / "sirius"
    persona_dir.mkdir(parents=True)

    response = await api_adapters_post(
        _FakeJsonRequest(
            {
                "adapters": [
                    {
                        "type": "napcat",
                        "qq_number": "10001",
                        "ws_url": "ws://localhost:3001",
                        "legacy_field_removed_long_ago": True,
                        "dispatch_priority": "urgent",
                    }
                ]
            }
        ),
        persona_dir,
    )

    assert response.status == 200
    saved = json.loads((persona_dir / "adapters.json").read_text(encoding="utf-8"))
    adapter = saved["adapters"][0]
    assert adapter["qq_number"] == "10001"
    assert "legacy_field_removed_long_ago" not in adapter
    assert adapter["dispatch_priority"] == 0.0


@pytest.mark.asyncio
async def test_experience_post_when_field_is_garbage_then_other_fields_still_save(tmp_path: Path):
    """一个坏字段不该让整份 experience 回退到默认值。"""
    from sirius_pulse.persona_config import PersonaExperienceConfig
    from sirius_pulse.webui.persona_api import api_experience_post

    persona_dir = tmp_path / "personas" / "sirius"
    persona_dir.mkdir(parents=True)

    response = await api_experience_post(
        _FakeJsonRequest({"engagement_sensitivity": "high", "max_tool_rounds": 25}), persona_dir
    )

    assert response.status == 200
    saved = PersonaExperienceConfig.load(persona_dir / "experience.json")
    assert saved.engagement_sensitivity == 0.5
    assert saved.max_tool_rounds == 25
