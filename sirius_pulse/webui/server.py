"""SiriusChat WebUI — 基于 aiohttp 的多人格配置管理面板。

提供 REST API + 内嵌前端页面，用于：
- 多个人格的列表、状态、启停管理
- 每人格的 Provider / 人格 / 模型编排 / Adapter / Experience 配置
- 每人格的 Skill 启停与配置管理

实现已拆分到：
  - server_core   : WebUIServer 类定义、路由、生命周期、全局 API
  - persona_api   : 人格列表/创建/删除/状态/启停/配置/访谈/模型编排/体验/Adapter/引擎重载
  - memory_api    : Token 统计（全局+人格）、认知事件、日记、向量存储状态、用户画像
  - server_skill_api: Skill 管理（已存在）

本模块作为向后兼容的 shim，重新导出 WebUIServer。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import json
import logging
from pathlib import Path
from typing import Any

from aiohttp import web

from sirius_pulse.webui.memory_api import (
    api_persona_cognition_analysis_get,
    api_persona_cognition_get,
    api_persona_conversation_history_delete,
    api_persona_conversation_history_get,
    api_persona_diary_delete,
    api_persona_diary_get,
    api_persona_diary_post,
    api_persona_diary_put,
    api_persona_glossary_delete,
    api_persona_glossary_get,
    api_persona_glossary_post,
    api_persona_glossary_put,
    api_persona_memory_unit_delete,
    api_persona_memory_unit_put,
    api_persona_memory_dedupe_apply,
    api_persona_memory_dedupe_report,
    api_persona_memory_dedupe_scan,
    api_persona_memory_dedupe_status,
    api_persona_memory_units_get,
    api_persona_memory_units_post,
    api_persona_memory_viz,
    api_persona_tokens_get,
    api_persona_user_delete,
    api_persona_user_get,
    api_persona_user_put,
    api_persona_users_get,
    api_persona_vector_store_status_get,
    api_telemetry_get,
    api_tokens_get,
)
from sirius_pulse.webui.monitoring_api import api_monitoring_health as _api_monitoring_health
from sirius_pulse.webui.monitoring_api import api_monitoring_overview as _api_monitoring_overview
from sirius_pulse.webui.monitoring_api import (
    api_monitoring_persona_metrics as _api_monitoring_persona_metrics,
)
from sirius_pulse.network.data_sync_api import (
    api_data_batch_post,
    api_data_glossary_post,
    api_data_messages_post,
    api_data_snapshot_get,
    api_data_snapshot_post,
    api_data_users_post,
)
from sirius_pulse.webui.persona_manager_api import (
    api_persona_activate,
    api_persona_active_get,
    api_persona_create,
    api_persona_delete,
    api_persona_start,
    api_persona_status,
    api_persona_stop,
    api_personas_list,
)
from sirius_pulse.webui.persona_api import (
    api_adapters_get,
    api_adapters_post,
    api_config_post,
    api_engine_reload,
    api_experience_get,
    api_experience_post,
    api_orchestration_get,
    api_orchestration_post,
    api_persona_get,
    api_persona_get_single,
    api_persona_interview,
    api_persona_interview_get,
    api_persona_logs_get,
    api_persona_post,
    api_persona_status_get,
    api_system_logs_get,
    api_task_params_get,
    api_task_params_post,
)
from sirius_pulse.webui.server_core import WebUIServer as _WebUIServer
from sirius_pulse.webui.server_plugin_api import (
    api_plugin_config_get,
    api_plugin_config_post,
    api_plugin_detail_get,
    api_plugin_monitor_repos_get,
    api_plugin_setting_delete,
    api_plugin_setting_post,
    api_plugin_settings_get,
    api_plugin_settings_post,
    api_plugin_toggle,
    api_plugins_get,
    api_plugins_reload,
)
from sirius_pulse.webui.server_skill_api import (
    api_persona_skill_config_get,
    api_persona_skill_config_post,
    api_persona_skill_history_get,
    api_persona_skill_toggle,
    api_persona_skills_get,
)
from sirius_pulse.webui.server_utils import _json_response

LOG = logging.getLogger("sirius.webui")

DelegatedHandler = Callable[[web.Request, Any], Awaitable[web.Response]]


DELEGATED_HANDLERS: dict[str, DelegatedHandler] = {
    "api_persona_get_single": api_persona_get_single,
    "api_persona_status_get": api_persona_status_get,
    "api_system_logs_get": api_system_logs_get,
    "api_persona_logs_get": api_persona_logs_get,
    "api_persona_get": api_persona_get,
    "api_persona_post": api_persona_post,
    "api_persona_interview_get": api_persona_interview_get,
    "api_persona_interview": api_persona_interview,
    "api_orchestration_get": api_orchestration_get,
    "api_orchestration_post": api_orchestration_post,
    "api_task_params_get": api_task_params_get,
    "api_task_params_post": api_task_params_post,
    "api_experience_get": api_experience_get,
    "api_experience_post": api_experience_post,
    "api_adapters_get": api_adapters_get,
    "api_adapters_post": api_adapters_post,
    "api_engine_reload": api_engine_reload,
    "api_tokens_get": api_tokens_get,
    "api_telemetry_get": api_telemetry_get,
    "api_persona_tokens_get": api_persona_tokens_get,
    "api_persona_cognition_get": api_persona_cognition_get,
    "api_persona_cognition_analysis_get": api_persona_cognition_analysis_get,
    "api_persona_diary_get": api_persona_diary_get,
    "api_persona_diary_post": api_persona_diary_post,
    "api_persona_diary_put": api_persona_diary_put,
    "api_persona_diary_delete": api_persona_diary_delete,
    "api_persona_vector_store_status_get": api_persona_vector_store_status_get,
    "api_persona_users_get": api_persona_users_get,
    "api_persona_user_get": api_persona_user_get,
    "api_persona_user_put": api_persona_user_put,
    "api_persona_user_delete": api_persona_user_delete,
    "api_persona_glossary_get": api_persona_glossary_get,
    "api_persona_glossary_post": api_persona_glossary_post,
    "api_persona_glossary_put": api_persona_glossary_put,
    "api_persona_glossary_delete": api_persona_glossary_delete,
    "api_persona_memory_units_get": api_persona_memory_units_get,
    "api_persona_memory_units_post": api_persona_memory_units_post,
    "api_persona_memory_unit_put": api_persona_memory_unit_put,
    "api_persona_memory_unit_delete": api_persona_memory_unit_delete,
    "api_persona_memory_dedupe_scan": api_persona_memory_dedupe_scan,
    "api_persona_memory_dedupe_status": api_persona_memory_dedupe_status,
    "api_persona_memory_dedupe_apply": api_persona_memory_dedupe_apply,
    "api_persona_memory_dedupe_report": api_persona_memory_dedupe_report,
    "api_persona_memory_viz": api_persona_memory_viz,
    "api_persona_conversation_history_delete": api_persona_conversation_history_delete,
    "api_persona_conversation_history_get": api_persona_conversation_history_get,
    "api_config_post": api_config_post,
    "api_plugins_get": api_plugins_get,
    "api_plugin_detail_get": api_plugin_detail_get,
    "api_plugin_toggle": api_plugin_toggle,
    "api_plugin_config_get": api_plugin_config_get,
    "api_plugin_config_post": api_plugin_config_post,
    "api_plugin_settings_get": api_plugin_settings_get,
    "api_plugin_settings_post": api_plugin_settings_post,
    "api_plugin_setting_post": api_plugin_setting_post,
    "api_plugin_setting_delete": api_plugin_setting_delete,
    "api_plugins_reload": api_plugins_reload,
    "api_plugin_monitor_repos_get": api_plugin_monitor_repos_get,
    "api_persona_skills_get": api_persona_skills_get,
    "api_persona_skill_toggle": api_persona_skill_toggle,
    "api_persona_skill_config_get": api_persona_skill_config_get,
    "api_persona_skill_config_post": api_persona_skill_config_post,
    "api_persona_skill_history_get": api_persona_skill_history_get,
    "api_monitoring_overview": _api_monitoring_overview,
    "api_monitoring_persona_metrics": _api_monitoring_persona_metrics,
    "api_monitoring_health": _api_monitoring_health,
    "api_data_snapshot_get": api_data_snapshot_get,
    "api_data_snapshot_post": api_data_snapshot_post,
    "api_data_messages_post": api_data_messages_post,
    "api_data_users_post": api_data_users_post,
    "api_data_glossary_post": api_data_glossary_post,
    "api_data_batch_post": api_data_batch_post,
    "api_personas_list": api_personas_list,
    "api_persona_create": api_persona_create,
    "api_persona_active_get": api_persona_active_get,
    "api_persona_activate": api_persona_activate,
    "api_persona_delete": api_persona_delete,
    "api_persona_start": api_persona_start,
    "api_persona_stop": api_persona_stop,
    "api_persona_status": api_persona_status,
}

# 人格作用域的 handler 前缀 — 这些 handler 的 data_dir 参数应传 persona_dir
_PERSONA_SCOPED_PREFIXES = (
    "api_persona_",
    "api_monitoring_",
    "api_data_",
    "api_memory_",
    "api_config_",
    "api_engine_",
    "api_orchestration_",
    "api_experience_",
    "api_adapters_",
    "api_task_",
    "api_tokens_",
    "api_telemetry_",
    "api_cognition_",
    "api_diary_",
    "api_vector_",
    "api_users_",
    "api_user_",
    "api_glossary_",
    "api_skill_",
    "api_conversations_",
    "api_knowledge_",
)

# 这些 handler 虽然以 api_persona_ 开头，但操作的是根目录（多人格管理），不传 persona_dir
_GLOBAL_PERSONA_HANDLERS = {
    "api_personas_list",
    "api_persona_create",
    "api_persona_active_get",
    "api_persona_activate",
    "api_persona_delete",
}


class WebUIServer(_WebUIServer):
    """WebUIServer with all API endpoints bound via mix-in overrides.

    Each handler delegates to the corresponding module-level async function.
    Persona-scoped handlers receive ``self.persona_dir`` (active persona's directory),
    global handlers receive ``self.data_dir`` (root data directory).
    """

    def __getattr__(self, name: str) -> Any:
        handler = DELEGATED_HANDLERS.get(name)
        if handler is None:
            raise AttributeError(name)

        # 判断是人格作用域还是全局作用域
        is_persona_scoped = name not in _GLOBAL_PERSONA_HANDLERS and any(
            name.startswith(p) for p in _PERSONA_SCOPED_PREFIXES
        )

        async def delegated(request: web.Request) -> web.Response:
            target_dir = self.persona_dir if is_persona_scoped else self.data_dir
            return await handler(request, target_dir)

        delegated.__name__ = name
        return delegated

    async def api_persona_activate(self, request):
        response = await api_persona_activate(request, self.data_dir)
        if response.status < 400:
            try:
                payload = json.loads(response.text)
                self._active_persona_name = str(payload.get("active") or "")
            except Exception:
                self._active_persona_name = str(request.match_info.get("name", ""))
        return response

    async def api_persona_start(self, request):
        target_dir = self.persona_dir
        response = await api_persona_start(request, target_dir)
        if response.status < 400:
            self._active_persona_name = target_dir.name
        return response

    async def api_auth_login(self, request):
        body = await request.json()
        username = str(body.get("username", ""))
        password = str(body.get("password", ""))
        token = self.auth_manager.authenticate(username, password)
        if token:
            return _json_response({"success": True, "token": token, "role": "admin"})
        return _json_response({"error": "用户名或密码错误"}, 401)

    async def api_auth_status(self, request):
        has_admin = bool(self.auth_manager._config.get("admin_password_hash"))
        return _json_response({"auth_enabled": has_admin})

    def _shutdown_persona_manager(self, persona_dir: Path) -> bool:
        manager = self.persona_manager
        if manager is None:
            return False

        manager_dir = getattr(manager, "persona_dir", None)
        if manager_dir is not None:
            try:
                if Path(manager_dir).resolve() != Path(persona_dir).resolve():
                    LOG.warning(
                        "跳过人格停止请求：manager=%s target=%s",
                        manager_dir,
                        persona_dir,
                    )
                    return False
            except Exception:
                return False

        shutdown = getattr(manager, "shutdown", None)
        if not callable(shutdown):
            return False
        shutdown()
        return True

    async def api_persona_stop(self, request):
        target_dir = self.persona_dir
        response = await api_persona_stop(request, target_dir)
        if response.status < 400 and target_dir.name == getattr(self, "_active_persona_name", ""):
            self._active_persona_name = ""
        if response.status < 400 and self._shutdown_persona_manager(target_dir):
            LOG.info("已请求停止人格 worker: %s", target_dir.name)
        return response

    async def api_shutdown(self, request):
        """关闭整个程序（WebUI + 引擎 + 所有服务）。"""
        import asyncio
        import os

        LOG.warning("收到关闭请求，正在停止所有服务...")
        # 延迟一小段时间让响应先发出
        asyncio.get_event_loop().call_later(0.5, lambda: os._exit(0))
        return _json_response({"success": True, "message": "正在关闭..."})


__all__ = ["WebUIServer"]
