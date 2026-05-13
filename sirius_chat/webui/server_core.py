"""WebUI server core: WebUIServer class, routes, lifecycle, global APIs."""

from __future__ import annotations

import json
import logging
import socket
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

from sirius_chat.providers.routing import WorkspaceProviderManager
from sirius_chat.webui.server_skill_api import (
    api_persona_skill_config_get,
    api_persona_skill_config_post,
    api_persona_skill_history_get,
    api_persona_skills_get,
    api_persona_skill_toggle,
)

LOG = logging.getLogger("sirius.webui")


def _json_response(data: dict[str, Any], status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False, indent=2))


def _get_name(request: web.Request) -> str:
    """从 URL 路径参数获取人格名称。"""
    return str(request.match_info.get("name", "")).strip()


@web.middleware
async def _no_cache_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
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
        napcat_manager: Any | None = None,
    ) -> None:
        self.persona_manager = persona_manager
        self.host = host
        self.port = port
        self.napcat_manager = napcat_manager
        self.app = web.Application(middlewares=[_no_cache_middleware])
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None
        self._embedding_thread: threading.Thread | None = None
        self._embedding_ready: bool = False  # 线程启动后模型是否加载成功
        self._embedding_error: str = ""  # 启动失败的具体原因
        self._embedding_port: int = int(
            persona_manager.global_config.get("embedding_port", 18900)
        )
        self._setup_routes()

    # ─── 子类 API 桩方法（Pylance 类型提示用，运行时由 server.py 覆盖）───

    if TYPE_CHECKING:
        async def api_napcat_status(self, request: web.Request) -> web.Response: ...
        async def api_napcat_install(self, request: web.Request) -> web.Response: ...
        async def api_napcat_configure(self, request: web.Request) -> web.Response: ...
        async def api_napcat_start(self, request: web.Request) -> web.Response: ...
        async def api_napcat_stop(self, request: web.Request) -> web.Response: ...
        async def api_napcat_logs(self, request: web.Request) -> web.Response: ...
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
        async def api_persona_diary_get(self, request: web.Request) -> web.Response: ...
        async def api_persona_vector_store_status_get(self, request: web.Request) -> web.Response: ...
        async def api_persona_users_get(self, request: web.Request) -> web.Response: ...
        async def api_persona_user_get(self, request: web.Request) -> web.Response: ...
        async def api_persona_glossary_get(self, request: web.Request) -> web.Response: ...
        async def api_config_post(self, request: web.Request) -> web.Response: ...
        async def api_persona_memory_viz(self, request: web.Request) -> web.Response: ...
        async def api_persona_stickers_get(self, request: web.Request) -> web.Response: ...
        async def api_persona_sticker_detail_get(self, request: web.Request) -> web.Response: ...
        async def api_persona_sticker_delete(self, request: web.Request) -> web.Response: ...
        async def api_plugins_get(self, request: web.Request) -> web.Response: ...
        async def api_plugin_detail_get(self, request: web.Request) -> web.Response: ...
        async def api_plugin_toggle(self, request: web.Request) -> web.Response: ...
        async def api_plugin_source_save(self, request: web.Request) -> web.Response: ...
        async def api_plugins_reload(self, request: web.Request) -> web.Response: ...

    def _setup_routes(self) -> None:
        self.app.router.add_get("/", self.index)
        self.app.router.add_static("/static/", Path(__file__).parent / "static", show_index=False)

        # 全局 API
        self.app.router.add_get("/api/global-config", self.api_global_config_get)
        self.app.router.add_post("/api/global-config", self.api_global_config_post)
        self.app.router.add_get("/api/providers", self.api_providers_get)
        self.app.router.add_post("/api/providers", self.api_providers_post)
        self.app.router.add_get("/api/models", self.api_available_models_get)
        self.app.router.add_get("/api/napcat/status", self.api_napcat_status)
        self.app.router.add_post("/api/napcat/install", self.api_napcat_install)
        self.app.router.add_post("/api/napcat/configure", self.api_napcat_configure)
        self.app.router.add_post("/api/napcat/start", self.api_napcat_start)
        self.app.router.add_post("/api/napcat/stop", self.api_napcat_stop)
        self.app.router.add_get("/api/napcat/logs", self.api_napcat_logs)
        self.app.router.add_get("/api/tokens", self.api_tokens_get)
        self.app.router.add_get("/api/telemetry", self.api_telemetry_get)
        self.app.router.add_get("/api/embedding/status", self.api_embedding_status)

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

        # 表情包管理（每人格独立）
        self.app.router.add_get("/api/personas/{name}/stickers", self.api_persona_stickers_get)
        self.app.router.add_get("/api/personas/{name}/stickers/{sticker_id}", self.api_persona_sticker_detail_get)
        self.app.router.add_delete("/api/personas/{name}/stickers/{sticker_id}", self.api_persona_sticker_delete)

        # Plugin 管理（全局，项目根 plugins/）
        self.app.router.add_get("/api/plugins", self.api_plugins_get)
        self.app.router.add_get("/api/plugins/{plugin_name}", self.api_plugin_detail_get)
        self.app.router.add_post("/api/plugins/{plugin_name}/toggle", self.api_plugin_toggle)
        self.app.router.add_put("/api/plugins/{plugin_name}/source", self.api_plugin_source_save)
        self.app.router.add_post("/api/plugins/reload", self.api_plugins_reload)

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
                pass
            LOG.warning(
                "Embedding 服务端口 %d 已被占用但不健康，可能有残留进程",
                self._embedding_port,
            )
            self._embedding_error = f"端口 {self._embedding_port} 已被占用且不可用"
            return

        def _run_server() -> None:
            import time as _time
            from sirius_chat.embedding.server import create_app
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
                pass

        for key in ("webui_host", "webui_port", "auto_manage_napcat", "log_level"):
            if key in body:
                data[key] = body[key]

        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        return _json_response({"success": True})

    # ─── 全局 API: Embedding 状态 ──────────────────────────

    async def api_embedding_status(self, request: web.Request) -> web.Response:
        return _json_response(self.get_embedding_status())

    # ─── 全局 API: Provider 配置 ──────────────────────────

    def _provider_keys_path(self) -> Path:
        return Path(self.persona_manager.data_path) / "providers" / "provider_keys.json"

    async def api_providers_get(self, request: web.Request) -> web.Response:
        path = self._provider_keys_path()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                # 脱敏：隐藏真实 key，只保留前 4 位
                providers: list[dict[str, Any]] = []
                raw_providers = data.get("providers", {}) if isinstance(data, dict) else {}
                for k, v in raw_providers.items():
                    if isinstance(v, dict):
                        key = v.get("api_key", "")
                        providers.append({
                            **v,
                            "name": k,
                            "api_key": key[:4] + "****" if len(key) > 4 else "****",
                        })
                return _json_response({"providers": providers})
            except Exception:
                pass
        return _json_response({"providers": []})

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
                pass

        providers_data = body.get("providers", {})
        if isinstance(providers_data, list):
            # 前端传的是数组格式，转换为 name -> config 的字典
            new_providers: dict[str, Any] = {}
            for cfg in providers_data:
                if isinstance(cfg, dict) and "name" in cfg:
                    name = cfg["name"]
                    new_providers[name] = {k: v for k, v in cfg.items() if k != "name"}
            providers_data = new_providers
        if isinstance(providers_data, dict):
            for provider, cfg in providers_data.items():
                if isinstance(cfg, dict):
                    if "providers" not in data:
                        data["providers"] = {}
                    if provider not in data["providers"]:
                        data["providers"][provider] = {}
                    for k, v in cfg.items():
                        # 如果前端传的是脱敏值，不覆盖原值
                        if k == "api_key" and isinstance(v, str) and "****" in v:
                            continue
                        data["providers"][provider][k] = v

        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        return _json_response({"success": True})

    # ─── 全局 API: 可用模型列表 ───────────────────────────

    def _build_model_choices(self) -> tuple[list[str], list[dict[str, str]]]:
        """返回 (available_models, model_choices)。"""
        available_models: list[str] = []
        model_choices: list[dict[str, str]] = []
        try:
            provider_mgr = WorkspaceProviderManager(self.persona_manager.data_path)
            for cfg in provider_mgr.load().values():
                if cfg.enabled:
                    for m in cfg.models:
                        available_models.append(m)
                        model_choices.append({
                            "label": f"{cfg.provider_type}/{m}",
                            "value": m,
                        })
            seen: set[str] = set()
            deduped_models: list[str] = []
            deduped_choices: list[dict[str, str]] = []
            for m, c in zip(available_models, model_choices):
                if m not in seen:
                    seen.add(m)
                    deduped_models.append(m)
                    deduped_choices.append(c)
            available_models = deduped_models
            model_choices = deduped_choices
        except Exception:
            pass
        return available_models, model_choices

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
