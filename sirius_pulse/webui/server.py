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
from typing import Any

from aiohttp import web

from sirius_pulse.webui.biography_api import (
    api_persona_biography_alias_index,
    api_persona_biography_alias_index_update,
    api_persona_biography_get,
    api_persona_biography_list,
)
from sirius_pulse.webui.evolution_api import (
    api_biography_list_all,
    api_biography_view,
)
from sirius_pulse.webui.evolution_api import (
    api_evolution_history,
    api_evolution_records,
    api_evolution_uncertain,
    api_knowledge_gaps,
    api_memory_dashboard,
)
from sirius_pulse.webui.memory_api import (
    api_persona_cognition_analysis_get,
    api_persona_cognition_get,
    api_persona_conversation_history_get,
    api_persona_diary_get,
    api_persona_glossary_get,
    api_persona_memory_viz,
    api_persona_tokens_get,
    api_persona_user_get,
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
from sirius_pulse.webui.persona_api import (
    api_adapters_get,
    api_adapters_post,
    api_config_post,
    api_engine_reload,
    api_experience_get,
    api_experience_post,
    api_orchestration_get,
    api_orchestration_post,
)
from sirius_pulse.webui.persona_api import api_persona_clone as _api_persona_clone
from sirius_pulse.webui.persona_api import (
    api_persona_get,
    api_persona_get_single,
    api_persona_interview,
    api_persona_interview_get,
    api_persona_logs_get,
    api_persona_post,
    api_persona_restart,
    api_persona_start,
    api_persona_status_get,
    api_persona_stop,
    api_personas_delete,
    api_personas_get,
    api_personas_post,
    api_system_logs_get,
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


DelegatedHandler = Callable[[web.Request, Any], Awaitable[web.Response]]


DELEGATED_HANDLERS: dict[str, DelegatedHandler] = {
    "api_personas_get": api_personas_get,
    "api_personas_post": api_personas_post,
    "api_personas_delete": api_personas_delete,
    "api_persona_get_single": api_persona_get_single,
    "api_persona_status_get": api_persona_status_get,
    "api_persona_start": api_persona_start,
    "api_persona_stop": api_persona_stop,
    "api_persona_restart": api_persona_restart,
    "api_system_logs_get": api_system_logs_get,
    "api_persona_logs_get": api_persona_logs_get,
    "api_persona_get": api_persona_get,
    "api_persona_post": api_persona_post,
    "api_persona_interview_get": api_persona_interview_get,
    "api_persona_interview": api_persona_interview,
    "api_orchestration_get": api_orchestration_get,
    "api_orchestration_post": api_orchestration_post,
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
    "api_persona_vector_store_status_get": api_persona_vector_store_status_get,
    "api_persona_users_get": api_persona_users_get,
    "api_persona_user_get": api_persona_user_get,
    "api_persona_glossary_get": api_persona_glossary_get,
    "api_persona_memory_viz": api_persona_memory_viz,
    "api_persona_conversation_history_get": api_persona_conversation_history_get,
    "api_memory_dashboard": api_memory_dashboard,
    "api_evolution_records": api_evolution_records,
    "api_evolution_history": api_evolution_history,
    "api_evolution_uncertain": api_evolution_uncertain,
    "api_biography_list_all": api_biography_list_all,
    "api_biography_view": api_biography_view,
    "api_knowledge_gaps": api_knowledge_gaps,
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
    "api_persona_biography_list": api_persona_biography_list,
    "api_persona_biography_get": api_persona_biography_get,
    "api_persona_biography_alias_index": api_persona_biography_alias_index,
    "api_persona_biography_alias_index_update": api_persona_biography_alias_index_update,
    "api_monitoring_overview": _api_monitoring_overview,
    "api_monitoring_persona_metrics": _api_monitoring_persona_metrics,
    "api_monitoring_health": _api_monitoring_health,
    "api_persona_clone": _api_persona_clone,
}


class WebUIServer(_WebUIServer):
    """WebUIServer with all API endpoints bound via mix-in overrides.

    Each handler delegates to the corresponding module-level async function,
    passing ``self.persona_manager`` (and ``self.napcat_manager`` where needed).
    """

    def __getattr__(self, name: str) -> Any:
        handler = DELEGATED_HANDLERS.get(name)
        if handler is None:
            raise AttributeError(name)

        async def delegated(request: web.Request) -> web.Response:
            return await handler(request, self.persona_manager)

        delegated.__name__ = name
        return delegated

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


__all__ = ["WebUIServer"]
