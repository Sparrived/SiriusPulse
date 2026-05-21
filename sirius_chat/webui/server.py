"""SiriusChat WebUI — 基于 aiohttp 的多人格配置管理面板。

提供 REST API + 内嵌前端页面，用于：
- 多个人格的列表、状态、启停管理
- 每人格的 Provider / 人格 / 模型编排 / Adapter / Experience 配置
- 全局 NapCat 管理
- 每人格的 Skill 启停与配置管理

实现已拆分到：
  - server_core   : WebUIServer 类定义、路由、生命周期、全局 API
  - persona_api   : 人格列表/创建/删除/状态/启停/配置/访谈/模型编排/体验/Adapter/引擎重载
  - memory_api    : Token 统计（全局+人格）、认知事件、日记、向量存储状态、用户画像
  - napcat_api    : NapCat 状态/安装/配置/启动/停止/日志
  - server_skill_api: Skill 管理（已存在）

本模块作为向后兼容的 shim，重新导出 WebUIServer。
"""

from __future__ import annotations

from sirius_chat.webui.server_core import WebUIServer as _WebUIServer
from sirius_chat.webui.persona_api import (
    api_personas_get,
    api_personas_post,
    api_personas_delete,
    api_persona_get_single,
    api_persona_status_get,
    api_persona_start,
    api_persona_stop,
    api_persona_restart,
    api_persona_get,
    api_persona_post,
    api_persona_interview_get,
    api_persona_interview,
    api_orchestration_get,
    api_orchestration_post,
    api_experience_get,
    api_experience_post,
    api_adapters_get,
    api_adapters_post,
    api_engine_reload,
    api_config_post,
)
from sirius_chat.webui.memory_api import (
    api_tokens_get,
    api_telemetry_get,
    api_persona_tokens_get,
    api_persona_cognition_get,
    api_persona_diary_get,
    api_persona_vector_store_status_get,
    api_persona_users_get,
    api_persona_user_get,
    api_persona_glossary_get,
    api_persona_memory_viz,
)
from sirius_chat.webui.biography_api import (
    api_persona_biography_list,
    api_persona_biography_get,
    api_persona_biography_alias_index,
    api_persona_biography_alias_index_update,
)
from sirius_chat.webui.napcat_api import (
    api_napcat_status,
    api_napcat_install,
    api_napcat_configure,
    api_napcat_logs,
    api_napcat_start,
    api_napcat_stop,
)
from sirius_chat.webui.server_skill_api import (
    api_persona_skills_get,
    api_persona_skill_toggle,
    api_persona_skill_config_get,
    api_persona_skill_config_post,
    api_persona_skill_history_get,
)
from sirius_chat.webui.server_plugin_api import (
    api_plugins_get,
    api_plugin_detail_get,
    api_plugin_toggle,
    api_plugin_config_get,
    api_plugin_config_post,
    api_plugin_settings_get,
    api_plugin_settings_post,
    api_plugin_setting_post,
    api_plugin_setting_delete,
    api_plugins_reload,
    api_plugin_monitor_repos_get,
)


class WebUIServer(_WebUIServer):
    """WebUIServer with all API endpoints bound via mix-in overrides.

    Each handler delegates to the corresponding module-level async function,
    passing ``self.persona_manager`` (and ``self.napcat_manager`` where needed).
    """

    # ─── 多人格 API: 列表 / 创建 / 删除 / 状态 ───────────────────

    async def api_personas_get(self, request):
        return await api_personas_get(request, self.persona_manager)

    async def api_personas_post(self, request):
        return await api_personas_post(request, self.persona_manager)

    async def api_personas_delete(self, request):
        return await api_personas_delete(request, self.persona_manager)

    async def api_persona_get_single(self, request):
        return await api_persona_get_single(request, self.persona_manager)

    async def api_persona_status_get(self, request):
        return await api_persona_status_get(request, self.persona_manager)

    async def api_persona_start(self, request):
        return await api_persona_start(request, self.persona_manager)

    async def api_persona_stop(self, request):
        return await api_persona_stop(request, self.persona_manager)

    async def api_persona_restart(self, request):
        return await api_persona_restart(request, self.persona_manager)

    # ─── 多人格 API: 人格配置 ─────────────────────────────

    async def api_persona_get(self, request):
        return await api_persona_get(request, self.persona_manager)

    async def api_persona_post(self, request):
        return await api_persona_post(request, self.persona_manager)

    async def api_persona_interview_get(self, request):
        return await api_persona_interview_get(request, self.persona_manager)

    async def api_persona_interview(self, request):
        return await api_persona_interview(request, self.persona_manager)

    async def api_orchestration_get(self, request):
        return await api_orchestration_get(request, self.persona_manager)

    async def api_orchestration_post(self, request):
        return await api_orchestration_post(request, self.persona_manager)

    async def api_experience_get(self, request):
        return await api_experience_get(request, self.persona_manager)

    async def api_experience_post(self, request):
        return await api_experience_post(request, self.persona_manager)

    async def api_adapters_get(self, request):
        return await api_adapters_get(request, self.persona_manager)

    async def api_adapters_post(self, request):
        return await api_adapters_post(request, self.persona_manager)

    async def api_engine_reload(self, request):
        return await api_engine_reload(request, self.persona_manager)

    # ─── 全局 API: Token / Telemetry ──────────────────────

    async def api_tokens_get(self, request):
        return await api_tokens_get(request, self.persona_manager)

    async def api_telemetry_get(self, request):
        return await api_telemetry_get(request, self.persona_manager)

    # ─── 多人格 API: Token / Cognition / Diary / Users ────

    async def api_persona_tokens_get(self, request):
        return await api_persona_tokens_get(request, self.persona_manager)

    async def api_persona_cognition_get(self, request):
        return await api_persona_cognition_get(request, self.persona_manager)

    async def api_persona_diary_get(self, request):
        return await api_persona_diary_get(request, self.persona_manager)

    async def api_persona_vector_store_status_get(self, request):
        return await api_persona_vector_store_status_get(request, self.persona_manager)

    async def api_persona_users_get(self, request):
        return await api_persona_users_get(request, self.persona_manager)

    async def api_persona_user_get(self, request):
        return await api_persona_user_get(request, self.persona_manager)

    async def api_persona_glossary_get(self, request):
        return await api_persona_glossary_get(request, self.persona_manager)

    async def api_persona_memory_viz(self, request):
        return await api_persona_memory_viz(request, self.persona_manager)

    # ─── 多人格 API: 桥接配置 ─────────────────────────────

    async def api_config_post(self, request):
        return await api_config_post(request, self.persona_manager)

    # ─── NapCat 管理 ──────────────────────────────────────

    async def api_napcat_status(self, request):
        return await api_napcat_status(request, self.napcat_manager)

    async def api_napcat_install(self, request):
        return await api_napcat_install(request, self.napcat_manager)

    async def api_napcat_configure(self, request):
        return await api_napcat_configure(request, self.napcat_manager)

    async def api_napcat_logs(self, request):
        return await api_napcat_logs(request, self.napcat_manager)

    async def api_napcat_start(self, request):
        return await api_napcat_start(request, self.napcat_manager)

    async def api_napcat_stop(self, request):
        return await api_napcat_stop(request, self.napcat_manager)

    # ─── Plugin 管理（全局） ──────────────────────────────

    async def api_plugins_get(self, request):
        return await api_plugins_get(request, self.persona_manager)

    async def api_plugin_detail_get(self, request):
        return await api_plugin_detail_get(request, self.persona_manager)

    async def api_plugin_toggle(self, request):
        return await api_plugin_toggle(request, self.persona_manager)

    async def api_plugin_config_get(self, request):
        return await api_plugin_config_get(request, self.persona_manager)

    async def api_plugin_config_post(self, request):
        return await api_plugin_config_post(request, self.persona_manager)

    async def api_plugin_settings_get(self, request):
        return await api_plugin_settings_get(request, self.persona_manager)

    async def api_plugin_settings_post(self, request):
        return await api_plugin_settings_post(request, self.persona_manager)

    async def api_plugin_setting_post(self, request):
        return await api_plugin_setting_post(request, self.persona_manager)

    async def api_plugin_setting_delete(self, request):
        return await api_plugin_setting_delete(request, self.persona_manager)

    async def api_plugins_reload(self, request):
        return await api_plugins_reload(request, self.persona_manager)

    async def api_plugin_monitor_repos_get(self, request):
        return await api_plugin_monitor_repos_get(request, self.persona_manager)

    # ─── Skill 管理 API 代理方法 ──────────────────────────
    # 这些方法将请求转发到 server_skill_api 模块，保持路由注册简洁

    async def api_persona_skills_get(self, request):
        return await api_persona_skills_get(request, self.persona_manager)

    async def api_persona_skill_toggle(self, request):
        return await api_persona_skill_toggle(request, self.persona_manager)

    async def api_persona_skill_config_get(self, request):
        return await api_persona_skill_config_get(request, self.persona_manager)

    async def api_persona_skill_config_post(self, request):
        return await api_persona_skill_config_post(request, self.persona_manager)

    async def api_persona_skill_history_get(self, request):
        return await api_persona_skill_history_get(request, self.persona_manager)

    # ─── 人物传记管理 API 代理方法 ──────────────────────────

    async def api_persona_biography_list(self, request):
        return await api_persona_biography_list(request, self.persona_manager)

    async def api_persona_biography_get(self, request):
        return await api_persona_biography_get(request, self.persona_manager)

    async def api_persona_biography_alias_index(self, request):
        return await api_persona_biography_alias_index(request, self.persona_manager)

    async def api_persona_biography_alias_index_update(self, request):
        return await api_persona_biography_alias_index_update(request, self.persona_manager)


__all__ = ["WebUIServer"]
