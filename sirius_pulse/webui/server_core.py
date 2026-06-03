"""WebUI server core: WebUIServer class, routes, lifecycle, global APIs."""

from __future__ import annotations

import json
import logging
import socket
import threading
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast

from aiohttp import web

from sirius_pulse.providers.base import AsyncLLMProvider
from sirius_pulse.providers.routing import WorkspaceProviderManager
from sirius_pulse.providers.routing import (
    ProviderConfig,
    _create_provider_instance,
    ensure_provider_platform_supported,
    probe_provider_availability,
)
from sirius_pulse.webui.auth import AuthManager
from sirius_pulse.webui.middleware import auth_middleware
from sirius_pulse.webui.server_skill_api import (
    api_persona_skill_config_get,
    api_persona_skill_config_post,
    api_persona_skill_history_get,
    api_persona_skills_get,
    api_persona_skill_toggle,
)
from sirius_pulse.webui.server_utils import _json_response
from sirius_pulse.webui.ws_server import WebSocketManager, setup_ws_routes

LOG = logging.getLogger("sirius.webui")


@web.middleware
async def _no_cache_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    """为静态文件禁用浏览器缓存。"""
    response = await handler(request)
    if request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


class WebUIServer:
    """aiohttp WebUI 服务器。"""

    def __init__(
        self,
        persona_manager: Any,
        host: str = "0.0.0.0",
        port: int = 8080,
    ) -> None:
        self.persona_manager = persona_manager
        self.host = host
        self.port = port
        self.ws_manager = WebSocketManager()
        self.auth_manager = AuthManager(Path(persona_manager.data_path))
        self.app = web.Application(middlewares=[auth_middleware, _no_cache_middleware])
        self.app["auth_manager"] = self.auth_manager
        self.app["ws_manager"] = self.ws_manager
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None
        self._embedding_thread: threading.Thread | None = None
        self._embedding_ready: bool = False
        self._embedding_error: str = ""
        self._embedding_port: int = int(
            persona_manager.global_config.get("embedding_port", 18900)
        )
        self.auth_manager.get_or_create_admin_password()
        self._setup_routes()
        setup_ws_routes(self.app, self.ws_manager)

    # ─── 子类 API 桩方法（Pylance 类型提示用，运行时由 server.py 覆盖）───

    if TYPE_CHECKING:
        async def api_tokens_get(self, request: web.Request) -> web.Response: ...
        async def api_telemetry_get(self, request: web.Request) -> web.Response: ...
        async def api_personas_get(self, request: web.Request) -> web.Response: ...
        async def api_personas_post(self, request: web.Request) -> web.Response: ...
        async def api_personas_delete(self, request: web.Request) -> web.Response: ...
        async def api_persona_get_single(self, request: web.Request) -> web.Response: ...
        async def api_persona_status_get(self, request: web.Request) -> web.Response: ...
        async def api_persona_start(self, request: web.Request) -> web.Response: ...
        async def api_persona_stop(self, request: web.Request) -> web.Response: ...
        async def api_persona_restart(self, request: web.Request) -> web.Response: ...
        async def api_persona_get(self, request: web.Request) -> web.Response: ...
        async def api_persona_post(self, request: web.Request) -> web.Response: ...
        async def api_persona_interview_get(self, request: web.Request) -> web.Response: ...
        async def api_persona_interview(self, request: web.Request) -> web.Response: ...
        async def api_orchestration_get(self, request: web.Request) -> web.Response: ...
        async def api_orchestration_post(self, request: web.Request) -> web.Response: ...
        async def api_experience_get(self, request: web.Request) -> web.Response: ...
        async def api_experience_post(self, request: web.Request) -> web.Response: ...
        async def api_adapters_get(self, request: web.Request) -> web.Response: ...
        async def api_adapters_post(self, request: web.Request) -> web.Response: ...
        async def api_engine_reload(self, request: web.Request) -> web.Response: ...
        async def api_persona_tokens_get(self, request: web.Request) -> web.Response: ...
        async def api_persona_cognition_get(self, request: web.Request) -> web.Response: ...
        async def api_persona_cognition_analysis_get(self, request: web.Request) -> web.Response: ...
        async def api_persona_diary_get(self, request: web.Request) -> web.Response: ...
        async def api_persona_vector_store_status_get(self, request: web.Request) -> web.Response: ...
        async def api_persona_users_get(self, request: web.Request) -> web.Response: ...
        async def api_persona_user_get(self, request: web.Request) -> web.Response: ...
        async def api_persona_glossary_get(self, request: web.Request) -> web.Response: ...
        async def api_config_post(self, request: web.Request) -> web.Response: ...
        async def api_persona_memory_viz(self, request: web.Request) -> web.Response: ...
        async def api_memory_dashboard(self, request: web.Request) -> web.Response: ...
        async def api_evolution_records(self, request: web.Request) -> web.Response: ...
        async def api_evolution_history(self, request: web.Request) -> web.Response: ...
        async def api_evolution_uncertain(self, request: web.Request) -> web.Response: ...
        async def api_memory_claims(self, request: web.Request) -> web.Response: ...
        async def api_memory_claim_provenance(self, request: web.Request) -> web.Response: ...
        async def api_situations_list(self, request: web.Request) -> web.Response: ...
        async def api_situations_delete(self, request: web.Request) -> web.Response: ...
        async def api_diary_slices(self, request: web.Request) -> web.Response: ...
        async def api_diary_slices_delete(self, request: web.Request) -> web.Response: ...
        async def api_biography_list_all(self, request: web.Request) -> web.Response: ...
        async def api_biography_view(self, request: web.Request) -> web.Response: ...
        async def api_knowledge_gaps(self, request: web.Request) -> web.Response: ...
        async def api_persona_conversation_history_get(self, request: web.Request) -> web.Response: ...
        async def api_persona_biography_list(self, request: web.Request) -> web.Response: ...
        async def api_persona_biography_get(self, request: web.Request) -> web.Response: ...
        async def api_persona_biography_alias_index(self, request: web.Request) -> web.Response: ...
        async def api_persona_biography_alias_index_update(self, request: web.Request) -> web.Response: ...
        async def api_plugins_get(self, request: web.Request) -> web.Response: ...
        async def api_plugin_detail_get(self, request: web.Request) -> web.Response: ...
        async def api_plugin_toggle(self, request: web.Request) -> web.Response: ...
        async def api_plugin_config_get(self, request: web.Request) -> web.Response: ...
        async def api_plugin_config_post(self, request: web.Request) -> web.Response: ...
        async def api_plugin_settings_get(self, request: web.Request) -> web.Response: ...
        async def api_plugin_settings_post(self, request: web.Request) -> web.Response: ...
        async def api_plugin_setting_post(self, request: web.Request) -> web.Response: ...
        async def api_plugin_setting_delete(self, request: web.Request) -> web.Response: ...
        async def api_plugins_reload(self, request: web.Request) -> web.Response: ...
        async def api_plugin_monitor_repos_get(self, request: web.Request) -> web.Response: ...
        async def api_persona_clone(self, request: web.Request) -> web.Response: ...
        async def api_auth_login(self, request: web.Request) -> web.Response: ...
        async def api_auth_status(self, request: web.Request) -> web.Response: ...
        async def api_monitoring_overview(self, request: web.Request) -> web.Response: ...
        async def api_monitoring_persona_metrics(self, request: web.Request) -> web.Response: ...
        async def api_monitoring_health(self, request: web.Request) -> web.Response: ...

    def _setup_routes(self) -> None:
        self.app.router.add_get("/", self.index)
        self.app.router.add_static("/static/", Path(__file__).parent / "static", show_index=False)

        # 全局 API
        self.app.router.add_get("/api/global-config", self.api_global_config_get)
        self.app.router.add_post("/api/global-config", self.api_global_config_post)
        self.app.router.add_get("/api/providers", self.api_providers_get)
        self.app.router.add_post("/api/providers", self.api_providers_post)
        self.app.router.add_post("/api/providers/probe", self.api_providers_probe)
        self.app.router.add_post("/api/providers/refresh-models", self.api_providers_refresh_models)
        self.app.router.add_get("/api/providers/models-dev/{provider_type}", self.api_providers_models_dev_get)
        self.app.router.add_get("/api/models", self.api_available_models_get)
        self.app.router.add_get("/api/tokens", self.api_tokens_get)
        self.app.router.add_get("/api/telemetry", self.api_telemetry_get)
        self.app.router.add_get("/api/embedding/status", self.api_embedding_status)
        self.app.router.add_post("/api/embedding/restart", self.api_embedding_restart)

        # 多人格 API: 列表 / 创建 / 删除 / 状态
        self.app.router.add_get("/api/personas", self.api_personas_get)
        self.app.router.add_post("/api/personas", self.api_personas_post)
        self.app.router.add_delete("/api/personas/{name}", self.api_personas_delete)
        self.app.router.add_get("/api/personas/{name}", self.api_persona_get_single)
        self.app.router.add_get("/api/personas/{name}/status", self.api_persona_status_get)
        self.app.router.add_post("/api/personas/{name}/start", self.api_persona_start)
        self.app.router.add_post("/api/personas/{name}/stop", self.api_persona_stop)
        self.app.router.add_post("/api/personas/{name}/restart", self.api_persona_restart)

        # 多人格 API: 配置
        self.app.router.add_get("/api/personas/{name}/persona", self.api_persona_get)
        self.app.router.add_post("/api/personas/{name}/persona", self.api_persona_post)
        self.app.router.add_post("/api/personas/{name}/persona/save", self.api_persona_post)
        self.app.router.add_get("/api/personas/{name}/persona/interview", self.api_persona_interview_get)
        self.app.router.add_post("/api/personas/{name}/persona/interview", self.api_persona_interview)
        self.app.router.add_get("/api/personas/{name}/orchestration", self.api_orchestration_get)
        self.app.router.add_post("/api/personas/{name}/orchestration", self.api_orchestration_post)
        self.app.router.add_get("/api/personas/{name}/experience", self.api_experience_get)
        self.app.router.add_post("/api/personas/{name}/experience", self.api_experience_post)
        self.app.router.add_get("/api/personas/{name}/adapters", self.api_adapters_get)
        self.app.router.add_post("/api/personas/{name}/adapters", self.api_adapters_post)

        # 多人格 API: 引擎控制
        self.app.router.add_post("/api/personas/{name}/engine/reload", self.api_engine_reload)

        # Token usage (per persona)
        self.app.router.add_get("/api/personas/{name}/tokens", self.api_persona_tokens_get)

        # Cognition events (per persona)
        self.app.router.add_get("/api/personas/{name}/cognition", self.api_persona_cognition_get)
        self.app.router.add_get("/api/personas/{name}/cognition/analysis", self.api_persona_cognition_analysis_get)

        # Diary entries (per persona)
        self.app.router.add_get("/api/personas/{name}/diary", self.api_persona_diary_get)
        self.app.router.add_get("/api/personas/{name}/vector-store-status", self.api_persona_vector_store_status_get)

        # User semantic profiles (per persona)
        self.app.router.add_get("/api/personas/{name}/users", self.api_persona_users_get)
        self.app.router.add_get("/api/personas/{name}/users/{user_id}", self.api_persona_user_get)

        # Glossary terms (per persona)
        self.app.router.add_get("/api/personas/{name}/glossary", self.api_persona_glossary_get)

        # 桥接配置（写入 adapters.json）
        self.app.router.add_post("/api/personas/{name}/config", self.api_config_post)

        # Skill 管理（每人格独立）
        self.app.router.add_get("/api/personas/{name}/skills", self.api_persona_skills_get)
        self.app.router.add_post("/api/personas/{name}/skills/{skill_name}/toggle", self.api_persona_skill_toggle)
        self.app.router.add_get("/api/personas/{name}/skills/{skill_name}/config", self.api_persona_skill_config_get)
        self.app.router.add_post("/api/personas/{name}/skills/{skill_name}/config", self.api_persona_skill_config_post)
        self.app.router.add_get("/api/personas/{name}/skill-history", self.api_persona_skill_history_get)

        # 记忆可视化
        self.app.router.add_get("/api/personas/{name}/memory-viz", self.api_persona_memory_viz)

        # 新记忆系统 API
        self.app.router.add_get("/api/personas/{name}/memory/dashboard", self.api_memory_dashboard)
        self.app.router.add_get("/api/personas/{name}/memory/evolution", self.api_evolution_records)
        self.app.router.add_get("/api/personas/{name}/memory/evolution/uncertain", self.api_evolution_uncertain)
        self.app.router.add_get("/api/personas/{name}/memory/evolution/{record_id}/history", self.api_evolution_history)
        self.app.router.add_get("/api/personas/{name}/memory/claims", self.api_memory_claims)
        self.app.router.add_get("/api/personas/{name}/memory/claims/{claim_id}/provenance", self.api_memory_claim_provenance)
        self.app.router.add_get("/api/personas/{name}/memory/situations", self.api_situations_list)
        self.app.router.add_delete("/api/personas/{name}/memory/situations", self.api_situations_delete)
        self.app.router.add_get("/api/personas/{name}/memory/diary-slices", self.api_diary_slices)
        self.app.router.add_delete("/api/personas/{name}/memory/diary-slices", self.api_diary_slices_delete)
        self.app.router.add_get("/api/personas/{name}/memory/biographies", self.api_biography_list_all)
        self.app.router.add_get("/api/personas/{name}/memory/biography/{user_id}", self.api_biography_view)
        self.app.router.add_get("/api/personas/{name}/memory/gaps/{user_id}", self.api_knowledge_gaps)

        # 对话历史
        self.app.router.add_get("/api/personas/{name}/conversations", self.api_persona_conversation_history_get)

        # 人物传记管理（每人格独立）
        self.app.router.add_get("/api/personas/{name}/biography", self.api_persona_biography_list)
        self.app.router.add_get("/api/personas/{name}/biography/aliases", self.api_persona_biography_alias_index)
        self.app.router.add_post("/api/personas/{name}/biography/aliases", self.api_persona_biography_alias_index_update)
        self.app.router.add_get("/api/personas/{name}/biography/{user_id}", self.api_persona_biography_get)

        # 认证 API
        self.app.router.add_post("/api/auth/login", self.api_auth_login)
        self.app.router.add_get("/api/auth/status", self.api_auth_status)

        # 监控 API
        self.app.router.add_get("/api/monitoring/overview", self.api_monitoring_overview)
        self.app.router.add_get("/api/monitoring/{name}/metrics", self.api_monitoring_persona_metrics)
        self.app.router.add_get("/api/monitoring/{name}/health", self.api_monitoring_health)

        # 人格克隆
        self.app.router.add_post("/api/personas/{name}/clone", self.api_persona_clone)

        # Plugin 管理（全局，项目根 plugins/）
        self.app.router.add_get("/api/plugins", self.api_plugins_get)
        self.app.router.add_get("/api/plugins/{plugin_name}", self.api_plugin_detail_get)
        self.app.router.add_post("/api/plugins/{plugin_name}/toggle", self.api_plugin_toggle)
        self.app.router.add_get("/api/plugins/{plugin_name}/config", self.api_plugin_config_get)
        self.app.router.add_put("/api/plugins/{plugin_name}/config", self.api_plugin_config_post)
        self.app.router.add_get("/api/plugins/{plugin_name}/settings", self.api_plugin_settings_get)
        self.app.router.add_post("/api/plugins/{plugin_name}/settings", self.api_plugin_settings_post)
        self.app.router.add_post("/api/plugins/{plugin_name}/settings/{key}", self.api_plugin_setting_post)
        self.app.router.add_delete("/api/plugins/{plugin_name}/settings/{key}", self.api_plugin_setting_delete)
        self.app.router.add_post("/api/plugins/reload", self.api_plugins_reload)
        self.app.router.add_get("/api/plugins/monitor_repos", self.api_plugin_monitor_repos_get)

    # ─── 生命周期 ─────────────────────────────────────────

    async def start(self) -> None:
        self._start_embedding_service()
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.host, self.port)
        await self.site.start()
        LOG.info("WebUI running on http://%s:%s", self.host, self.port)

    async def stop(self) -> None:
        if self.site:
            await self.site.stop()
        if self.runner:
            await self.runner.cleanup()
        self._stop_embedding_service()
        LOG.info("WebUI stopped")

    # ─── Embedding 服务管理 ────────────────────────────────

    @staticmethod
    def _is_port_free(port: int) -> bool:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("localhost", port))
                return True
        except OSError:
            return False

    def get_embedding_status(self) -> dict[str, Any]:
        """返回 embedding 服务的真实健康状态。"""
        # 线程不存在 → 未启动
        if self._embedding_thread is None:
            return {"running": False, "ready": False, "error": self._embedding_error or "未启动"}
        # 线程已死亡（启动失败）→ 检查是否还在运行
        if not self._embedding_thread.is_alive():
            return {
                "running": False,
                "ready": False,
                "error": self._embedding_error or "服务线程已退出",
            }
        # 线程存活但模型还没加载完
        if not self._embedding_ready:
            return {"running": True, "ready": False, "error": "模型加载中..."}
        return {"running": True, "ready": True, "error": ""}

    def _start_embedding_service(self) -> None:
        if self._embedding_thread is not None:
            LOG.warning("Embedding 服务已在运行")
            return

        if not self._is_port_free(self._embedding_port):
            # 端口被占用：可能是外部已启动，尝试健康检查
            import urllib.request
            import json as _json
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{self._embedding_port}/health", method="GET"
                )
                with urllib.request.urlopen(req, timeout=2.0) as resp:
                    data = _json.loads(resp.read().decode("utf-8"))
                    if data.get("status") == "ok":
                        self._embedding_ready = True
                        LOG.info(
                            "Embedding 服务端口 %d 已被外部进程占用且健康，跳过内部启动",
                            self._embedding_port,
                        )
                        return
            except Exception:
                LOG.warning("Embedding 服务健康检查失败", exc_info=True)
                pass
            LOG.warning(
                "Embedding 服务端口 %d 已被占用但不健康，可能有残留进程",
                self._embedding_port,
            )
            self._embedding_error = f"端口 {self._embedding_port} 已被占用且不可用"
            return

        def _run_server() -> None:
            import time as _time
            from sirius_pulse.embedding.server import create_app
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    app = create_app()
                    self._embedding_ready = True
                    if attempt == 0:
                        LOG.info("Embedding 模型加载完成，启动 HTTP 服务...")
                    else:
                        LOG.info("Embedding 服务重启 (第 %d 次)", attempt)
                    # 显式绑定 127.0.0.1，确保子进程通过 localhost/127.0.0.1 可访问
                    web.run_app(app, host="127.0.0.1", port=self._embedding_port, print=None)
                    break  # 正常退出（不应发生）
                except Exception as exc:
                    self._embedding_error = str(exc)
                    self._embedding_ready = False
                    LOG.error("Embedding 服务异常 (第 %d/%d 次): %s", attempt + 1, max_retries, exc)
                    if attempt < max_retries - 1:
                        _time.sleep(5)

        self._embedding_thread = threading.Thread(
            target=_run_server, daemon=True, name="embedding-server"
        )
        self._embedding_thread.start()
        LOG.info("Embedding 服务后台线程已启动 (host=127.0.0.1 port=%d)", self._embedding_port)

    def _stop_embedding_service(self) -> None:
        if self._embedding_thread is not None:
            LOG.info("Embedding 服务线程将随主进程退出")
            self._embedding_thread = None

    # ─── 静态页面 ─────────────────────────────────────────

    async def index(self, request: web.Request) -> web.StreamResponse:
        html_path = Path(__file__).parent / "static" / "index.html"
        if html_path.exists():
            return web.FileResponse(html_path)
        return web.Response(text="WebUI not found", status=404)

    # ─── 全局 API: 全局配置 ───────────────────────────────

    def _global_config_path(self) -> Path:
        return Path(self.persona_manager.data_path) / "global_config.json"

    async def api_global_config_get(self, request: web.Request) -> web.Response:
        path = self._global_config_path()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return _json_response(data)
            except Exception:
                LOG.warning("读取全局配置失败", exc_info=True)
                pass
        return _json_response({
            "webui_host": self.host,
            "webui_port": self.port,
            "auto_manage_napcat": True,
            "log_level": "INFO",
        })

    async def api_global_config_post(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return _json_response({"error": "Invalid JSON"}, 400)

        path = self._global_config_path()
        data: dict[str, Any] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                LOG.warning("读取全局配置失败", exc_info=True)
                pass

        for key in ("webui_host", "webui_port", "auto_manage_napcat", "log_level"):
            if key in body:
                data[key] = body[key]

        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

        # 通知所有运行中的人格热重载 provider 配置
        self._notify_provider_reload()

        return _json_response({"success": True})

    # ─── 全局 API: Embedding 状态 ──────────────────────────

    async def api_embedding_status(self, request: web.Request) -> web.Response:
        return _json_response(self.get_embedding_status())

    async def api_embedding_restart(self, request: web.Request) -> web.Response:
        LOG.info("收到 Embedding 服务重启请求")
        self._stop_embedding_service()
        self._embedding_ready = False
        self._embedding_error = ""
        import time as _time
        _time.sleep(1)
        self._start_embedding_service()
        import asyncio as _aio
        for _ in range(30):
            await _aio.sleep(1)
            if self._embedding_ready:
                return _json_response({"success": True, "ready": True})
            if self._embedding_error:
                return _json_response({"success": False, "error": self._embedding_error})
        return _json_response({"success": False, "error": "启动超时"})

    # ─── 全局 API: Provider 配置 ──────────────────────────

    def _provider_keys_path(self) -> Path:
        return Path(self.persona_manager.data_path) / "providers" / "provider_keys.json"

    def _notify_provider_reload(self) -> None:
        """向所有运行中的人格写入 provider 重载标志。"""
        for info in self.persona_manager.list_personas():
            if not info.get("running"):
                continue
            paths = self.persona_manager.get_persona_paths(info["name"])
            if paths is None:
                continue
            try:
                flag = paths.engine_state / "reload_requested"
                flag.parent.mkdir(parents=True, exist_ok=True)
                flag.write_text("provider", encoding="utf-8")
                LOG.debug("已向人格 %s 写入 provider 重载标志", info["name"])
            except Exception as exc:
                LOG.debug("向人格 %s 写入 provider 重载标志失败: %s", info["name"], exc)

    async def api_providers_get(self, request: web.Request) -> web.Response:
        return _json_response({"providers": self._load_providers_raw()})

    async def api_providers_post(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return _json_response({"error": "Invalid JSON"}, 400)

        path = self._provider_keys_path()
        data: dict[str, Any] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                LOG.warning("读取全局配置失败", exc_info=True)
                pass

        providers_data = body.get("providers", {})
        if isinstance(providers_data, list):
            # 前端传的是数组格式，转换为 name -> config 的字典
            new_providers: dict[str, Any] = {}
            for cfg in providers_data:
                if isinstance(cfg, dict):
                    if "name" in cfg:
                        name = cfg["name"]
                    else:
                        name = cfg.get("type", "unnamed")
                        if name in new_providers:
                            name = f"{name}-{len(new_providers)}"
                    new_providers[name] = {k: v for k, v in cfg.items() if k != "name"}
            providers_data = new_providers
        if isinstance(providers_data, dict):
            if "providers" not in data:
                data["providers"] = {}
            for provider, cfg in providers_data.items():
                if isinstance(cfg, dict):
                    if provider not in data["providers"]:
                        data["providers"][provider] = {}
                    for k, v in cfg.items():
                        if k == "api_key" and isinstance(v, str) and "****" in v:
                            continue
                        data["providers"][provider][k] = v
            saved_names = set(providers_data.keys())
            for old_name in list(data["providers"].keys()):
                if old_name not in saved_names:
                    del data["providers"][old_name]

        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        return _json_response({"success": True})

    # ─── 全局 API: Provider 健康检查 ───────────────────────

    async def api_providers_probe(self, request: web.Request) -> web.Response:
        """对单个 Provider 执行健康检查（发送 ping 请求验证连通性）。"""
        import time

        try:
            body = await request.json()
        except Exception:
            return _json_response({"error": "Invalid JSON"}, 400)

        provider_name = str(body.get("name", "")).strip()
        if not provider_name:
            return _json_response({"error": "缺少 name 参数"}, 400)

        path = self._provider_keys_path()
        if not path.exists():
            return _json_response({"error": "Provider 配置文件不存在"}, 404)

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return _json_response({"error": "读取 Provider 配置失败"}, 500)

        raw_providers = data.get("providers", {}) if isinstance(data, dict) else {}
        provider_cfg = raw_providers.get(provider_name)
        if not provider_cfg or not isinstance(provider_cfg, dict):
            return _json_response({"error": f"未找到 Provider: {provider_name}"}, 404)

        provider_type = str(provider_cfg.get("type", "") or provider_cfg.get("platform_type", "") or provider_name).strip()
        api_key = str(provider_cfg.get("api_key", "")).strip()
        base_url = str(provider_cfg.get("base_url", "")).strip()
        healthcheck_model = str(provider_cfg.get("healthcheck_model", "")).strip()

        if not api_key:
            return _json_response({"error": "该 Provider 未配置 API Key"}, 400)
        if not healthcheck_model:
            return _json_response({"error": "该 Provider 未配置 healthcheck_model"}, 400)

        try:
            normalized_type = ensure_provider_platform_supported(provider_type)
        except Exception as exc:
            return _json_response({"error": str(exc)}, 400)

        config = ProviderConfig(
            provider_type=normalized_type,
            api_key=api_key,
            base_url=base_url,
            healthcheck_model=healthcheck_model,
            enabled=True,
        )

        try:
            provider = cast(AsyncLLMProvider, _create_provider_instance(config))
        except Exception as exc:
            return _json_response({"error": f"创建 Provider 实例失败: {exc}"}, 500)

        t0 = time.monotonic()
        try:
            await probe_provider_availability(provider=provider, model_name=healthcheck_model)
            latency = round((time.monotonic() - t0) * 1000)
            return _json_response({"success": True, "latency_ms": latency})
        except Exception as exc:
            latency = round((time.monotonic() - t0) * 1000)
            return _json_response({"success": False, "error": str(exc), "latency_ms": latency})

    async def api_providers_refresh_models(self, request: web.Request) -> web.Response:
        """从 models.dev 刷新模型数据。

        当 body 中 ``cache_only`` 为 true 时，只刷新 models.dev 缓存，
        不自动合并模型到各 provider（用户通过编辑 UI 自行选择添加）。
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        force = bool(body.get("force", False))
        cache_only = bool(body.get("cache_only", False))

        try:
            if cache_only:
                # 只刷新 models.dev 缓存，不修改 provider 配置
                from sirius_pulse.providers.models_dev import ModelsDevCache

                cache = ModelsDevCache(Path(self.persona_manager.data_path))
                cache.get(force_refresh=True)
                providers_data = self._load_providers_raw()
                return _json_response({
                    "success": True,
                    "changed": False,
                    "providers": providers_data,
                })

            provider_mgr = WorkspaceProviderManager(self.persona_manager.data_path)
            changed = provider_mgr.refresh_models_from_dev(force=force)
            if changed:
                self._notify_provider_reload()
            # 重新加载并返回更新后的 provider 列表
            providers_data = self._load_providers_raw()
            return _json_response({
                "success": True,
                "changed": changed,
                "providers": providers_data,
            })
        except Exception as exc:
            LOG.warning("刷新模型列表失败", exc_info=True)
            return _json_response({"success": False, "error": str(exc)})

    async def api_providers_models_dev_get(self, request: web.Request) -> web.Response:
        """返回指定 provider 类型在 models.dev 上的可用模型列表（含能力属性）。"""
        provider_type = request.match_info.get("provider_type", "").strip()
        if not provider_type:
            return _json_response({"error": "缺少 provider_type"}, 400)

        from sirius_pulse.providers.models_dev import (
            ModelsDevCache,
            list_provider_model_details,
        )

        cache = ModelsDevCache(Path(self.persona_manager.data_path))
        data = cache.get()
        if not data:
            return _json_response({"error": "无法获取 models.dev 数据"}, 502)

        models = list_provider_model_details(data, provider_type)
        return _json_response({"provider_type": provider_type, "models": models})

    def _load_providers_raw(self) -> list[dict[str, Any]]:
        """读取 provider 列表（API Key 脱敏），供多个端点复用。"""
        path = self._provider_keys_path()
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            providers: list[dict[str, Any]] = []
            raw_providers = data.get("providers", {}) if isinstance(data, dict) else {}
            for k, v in raw_providers.items():
                if isinstance(v, dict):
                    key = str(v.get("api_key", "")).strip()
                    masked = key[:4] + "****" if len(key) > 4 else ("****" if key else "")
                    providers.append({**v, "name": k, "api_key": masked})
            return providers
        except Exception:
            LOG.warning("读取 provider_keys 失败", exc_info=True)
            return []

    # ─── 全局 API: 可用模型列表 ───────────────────────────

    def _build_model_choices(self) -> tuple[list[str], list[dict[str, str]]]:
        """返回 (available_models, model_choices)。

        model_choices 的 value 使用复合格式 ``{provider_type}/{model_name}``
        以区分来自不同 provider 的同名模型。``available_models`` 保留裸模型名
        用于下游引擎调用。
        """
        available_models: list[str] = []
        model_choices: list[dict[str, str]] = []
        seen_models: set[str] = set()
        try:
            provider_mgr = WorkspaceProviderManager(self.persona_manager.data_path)
            for cfg in provider_mgr.load().values():
                if cfg.enabled:
                    for m in cfg.models:
                        # available_models 用裸模型名（引擎直接透传给 API）
                        if m not in seen_models:
                            seen_models.add(m)
                            available_models.append(m)
                        # model_choices 用复合值，不同 provider 的同名模型各自独立
                        composite = f"{cfg.provider_type}/{m}"
                        model_choices.append({
                            "label": composite,
                            "value": composite,
                        })
        except Exception:
            LOG.warning("获取模型列表失败", exc_info=True)
            pass
        # 从 models.dev 注入能力标签
        self._enrich_model_choices(model_choices)
        return available_models, model_choices

    def _enrich_model_choices(self, model_choices: list[dict[str, Any]]) -> None:
        """为 model_choices 中的每一项注入 models.dev 能力标签。"""
        from sirius_pulse.providers.models_dev import ModelsDevCache, get_provider_models
        try:
            cache = ModelsDevCache(Path(self.persona_manager.data_path))
            data = cache.get()
            if not data:
                return
            all_models: dict[str, dict[str, object]] = {}
            for prov in data.values():
                if isinstance(prov, dict):
                    for mid, mobj in prov.get("models", {}).items():
                        if isinstance(mobj, dict) and mid not in all_models:
                            all_models[mid] = mobj
            for choice in model_choices:
                # value 为复合格式 provider_type/model_name，提取裸模型名查询标签
                raw_val = choice["value"]
                model_id = raw_val.split("/", 1)[1] if "/" in raw_val else raw_val
                m = all_models.get(model_id)
                if not m:
                    continue
                tags: list[str] = []
                if m.get("tool_call"):
                    tags.append("函数调用")
                if m.get("reasoning"):
                    tags.append("推理")
                if m.get("structured_output"):
                    tags.append("结构化")
                modalities = m.get("modalities", {})
                input_mods = modalities.get("input", []) if isinstance(modalities, dict) else []
                if "image" in input_mods:
                    tags.append("视觉")
                if "audio" in input_mods:
                    tags.append("音频")
                if tags:
                    choice["tags"] = tags
        except Exception:
            LOG.debug("注入模型能力标签失败", exc_info=True)

    async def api_available_models_get(self, request: web.Request) -> web.Response:
        """返回全局可用模型列表（含 provider 前缀显示名）。"""
        available_models, model_choices = self._build_model_choices()
        return _json_response({
            "available_models": available_models,
            "model_choices": model_choices,
        })

    # ─── Skill 管理 API 代理方法 ──────────────────────────

    async def api_persona_skills_get(self, request: web.Request) -> web.Response:
        return await api_persona_skills_get(request, self.persona_manager)

    async def api_persona_skill_toggle(self, request: web.Request) -> web.Response:
        return await api_persona_skill_toggle(request, self.persona_manager)

    async def api_persona_skill_config_get(self, request: web.Request) -> web.Response:
        return await api_persona_skill_config_get(request, self.persona_manager)

    async def api_persona_skill_config_post(self, request: web.Request) -> web.Response:
        return await api_persona_skill_config_post(request, self.persona_manager)

    async def api_persona_skill_history_get(self, request: web.Request) -> web.Response:
        return await api_persona_skill_history_get(request, self.persona_manager)
