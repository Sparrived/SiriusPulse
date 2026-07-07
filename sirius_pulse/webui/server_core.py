"""WebUI server core: WebUIServer class, routes, lifecycle, global APIs."""

from __future__ import annotations

import json
import logging
import socket
import multiprocessing
import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

from aiohttp import web

from sirius_pulse.providers.base import AsyncLLMProvider
from sirius_pulse.providers.routing import (
    ProviderConfig,
    WorkspaceProviderManager,
    _create_provider_instance,
    ensure_provider_platform_supported,
    probe_provider_availability,
)
from sirius_pulse.webui.app_keys import AUTH_MANAGER_KEY, DATA_DIR_KEY, WS_MANAGER_KEY
from sirius_pulse.webui.auth import AuthManager
from sirius_pulse.webui.middleware import auth_middleware
from sirius_pulse.webui.model_catalog import build_model_catalog
from sirius_pulse.webui.routes import WEBUI_ROUTES
from sirius_pulse.webui.server_utils import _json_response
from sirius_pulse.webui.ws_server import WebSocketManager, WebUIFileEventBridge, setup_ws_routes

LOG = logging.getLogger("sirius.webui")


def _run_embedding_server_process(port: int) -> None:
    """Run the embedding HTTP server in a child process."""
    import time as _time

    from sirius_pulse.embedding.server import create_app

    max_retries = 3
    for attempt in range(max_retries):
        try:
            app = create_app()
            if attempt == 0:
                LOG.info("Embedding model loaded; starting HTTP service")
            else:
                LOG.info("Restarting embedding service (attempt %d)", attempt)
            web.run_app(app, host="127.0.0.1", port=port, print=None)
            break
        except Exception as exc:
            LOG.error("Embedding service failed (attempt %d/%d): %s", attempt + 1, max_retries, exc)
            if attempt < max_retries - 1:
                _time.sleep(5)


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
        data_dir: Path,
        host: str = "0.0.0.0",
        port: int = 8080,
        persona_manager: Any = None,
    ) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.persona_manager = persona_manager
        self.host = host
        self.port = port
        self.ws_manager = WebSocketManager()
        self.file_event_bridge = WebUIFileEventBridge(self.data_dir, self.ws_manager)
        self.auth_manager = AuthManager(self.data_dir)
        self.app = web.Application(middlewares=[auth_middleware, _no_cache_middleware])
        self.app[DATA_DIR_KEY] = self.data_dir
        self.app[AUTH_MANAGER_KEY] = self.auth_manager
        self.app[WS_MANAGER_KEY] = self.ws_manager
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None
        self._embedding_process: multiprocessing.Process | None = None
        self._embedding_ready: bool = False
        self._embedding_error: str = ""
        self._embedding_port: int = 18900
        self._load_global_config()
        self.auth_manager.get_or_create_admin_password()
        self._setup_routes()
        setup_ws_routes(self.app, self.ws_manager)

    def _load_global_config(self) -> None:
        """从 global_config.json 读取全局配置。"""
        path = self._global_config_path()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._embedding_port = int(data.get("embedding_port", 18900))
                    self._active_persona_name = data.get("active_persona", "")
            except Exception:
                LOG.warning("读取全局配置失败", exc_info=True)

    # ─── 人格目录解析 ─────────────────────────────────────

    @property
    def persona_dir(self) -> Path:
        """当前活跃人格的目录路径。"""
        name = getattr(self, "_active_persona_name", "")
        if not name:
            # 兼容旧格式：如果没有 active_persona，直接用 data_dir
            if (self.data_dir / "persona.json").exists():
                return self.data_dir
            # 尝试取第一个 persona
            personas_dir = self.data_dir / "personas"
            if personas_dir.exists():
                for d in sorted(personas_dir.iterdir()):
                    if d.is_dir() and (d / "persona.json").exists():
                        return d
            return self.data_dir
        return self.data_dir / "personas" / name

    def get_persona_dir(self, name: str) -> Path:
        """获取指定人格的目录路径。"""
        return self.data_dir / "personas" / name

    def list_personas(self) -> list[dict[str, str]]:
        """列出所有人格。"""
        personas_dir = self.data_dir / "personas"
        if not personas_dir.exists():
            return []
        result = []
        active = getattr(self, "_active_persona_name", "")
        for d in sorted(personas_dir.iterdir()):
            if not d.is_dir():
                continue
            persona_file = d / "persona.json"
            display_name = d.name
            if persona_file.exists():
                try:
                    data = json.loads(persona_file.read_text(encoding="utf-8"))
                    display_name = data.get("name", d.name)
                except Exception:
                    pass
            result.append(
                {
                    "name": d.name,
                    "display_name": display_name,
                    "active": d.name == active,
                }
            )
        return result

    def _setup_routes(self) -> None:
        self.app.router.add_get("/", self.index)
        self.app.router.add_static("/static/", Path(__file__).parent / "static", show_index=False)
        for spec in WEBUI_ROUTES:
            self.app.router.add_route(spec.method, spec.path, getattr(self, spec.handler_name))

    # ─── 生命周期 ─────────────────────────────────────────

    async def start(self) -> None:
        self._start_embedding_service()
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.host, self.port)
        await self.site.start()
        self.file_event_bridge.start(asyncio.get_running_loop())
        LOG.info("WebUI running on http://%s:%s", self.host, self.port)

    async def stop(self) -> None:
        self.file_event_bridge.stop()
        await self.ws_manager.close_all()
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
        if self._embedding_process is None:
            return {"running": False, "ready": False, "error": self._embedding_error or "未启动"}
        # 线程已死亡（启动失败）→ 检查是否还在运行
        if not self._embedding_process.is_alive():
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
        if self._embedding_process is not None:
            LOG.warning("Embedding 服务已在运行")
            return

        if not self._is_port_free(self._embedding_port):
            # 端口被占用：可能是外部已启动，尝试健康检查
            import json as _json
            import urllib.request

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

        self._embedding_process = multiprocessing.Process(
            target=_run_embedding_server_process,
            args=(self._embedding_port,),
            daemon=True,
            name="embedding-server",
        )
        self._embedding_process.start()
        LOG.info("Embedding 服务后台线程已启动 (host=127.0.0.1 port=%d)", self._embedding_port)

    def _stop_embedding_service(self) -> None:
        if self._embedding_process is not None:
            LOG.info("Embedding 服务线程将随主进程退出")
            self._embedding_process = None

    # ─── 静态页面 ─────────────────────────────────────────

    async def index(self, request: web.Request) -> web.StreamResponse:
        html_path = Path(__file__).parent / "static" / "index.html"
        if html_path.exists():
            return web.FileResponse(html_path)
        return web.Response(text="WebUI not found", status=404)

    # ─── 全局 API: 全局配置 ───────────────────────────────

    def _global_config_path(self) -> Path:
        return self.data_dir / "global_config.json"

    async def api_global_config_get(self, request: web.Request) -> web.Response:
        path = self._global_config_path()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return _json_response(data)
            except Exception:
                LOG.warning("读取全局配置失败", exc_info=True)
                pass
        return _json_response(
            {
                "webui_host": self.host,
                "webui_port": self.port,
                "log_level": "INFO",
            }
        )

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

        for key in ("webui_host", "webui_port", "log_level"):
            if key in body:
                data[key] = body[key]

        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

        # 通知当前运行中的人格热重载全局运行时配置
        self._notify_config_reload("global")

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
        return WorkspaceProviderManager(self.data_dir).path

    @staticmethod
    def _mask_api_key(api_key: Any) -> str:
        key = str(api_key or "").strip()
        if not key:
            return ""
        return key[:4] + "****" if len(key) > 4 else "****"

    @staticmethod
    def _provider_mapping_from_payload(payload: Any) -> dict[str, dict[str, Any]]:
        if isinstance(payload, dict) and "providers" in payload:
            payload = payload["providers"]

        if isinstance(payload, dict):
            providers: dict[str, dict[str, Any]] = {}
            for name, cfg in payload.items():
                if isinstance(cfg, dict):
                    providers[str(name)] = dict(cfg)
            return providers

        if isinstance(payload, list):
            providers = {}
            for idx, cfg in enumerate(payload):
                if not isinstance(cfg, dict):
                    continue
                name = str(
                    cfg.get("name")
                    or cfg.get("type")
                    or cfg.get("platform_type")
                    or f"provider-{idx}"
                ).strip()
                if not name:
                    name = f"provider-{idx}"
                if name in providers:
                    name = f"{name}-{idx}"
                providers[name] = {k: v for k, v in cfg.items() if k != "name"}
            return providers

        return {}

    def _notify_provider_reload(self) -> None:
        """向当前人格写入 provider 重载标志。"""
        self._notify_config_reload("provider")

    def _notify_config_reload(self, reload_type: str) -> None:
        """向当前人格写入配置重载标志，并合并快速连续请求。"""
        try:
            flag = self.persona_dir / "engine_state" / "reload_requested"
            flag.parent.mkdir(parents=True, exist_ok=True)
            types: set[str] = set()
            if flag.exists():
                raw = flag.read_text(encoding="utf-8").strip()
                try:
                    existing = json.loads(raw)
                    if isinstance(existing, dict):
                        types.update(str(item) for item in existing.get("types", []))
                    elif isinstance(existing, list):
                        types.update(str(item) for item in existing)
                    elif raw:
                        types.add(raw)
                except Exception:
                    if raw:
                        types.add(raw)
            types.add(reload_type)
            payload = (
                next(iter(types))
                if len(types) == 1
                else json.dumps({"types": sorted(types)}, ensure_ascii=False)
            )
            flag.write_text(payload, encoding="utf-8")
            LOG.debug("已写入配置重载标志: %s", sorted(types))
        except Exception as exc:
            LOG.debug("写入配置重载标志失败: %s", exc)

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

        existing_providers = self._provider_mapping_from_payload(data)
        incoming_providers = self._provider_mapping_from_payload(body.get("providers", {}))
        saved_providers: dict[str, dict[str, Any]] = {}
        for provider, cfg in incoming_providers.items():
            existing = existing_providers.get(provider, {})
            saved = dict(existing)
            provider_type = str(
                cfg.get("type")
                or cfg.get("platform_type")
                or existing.get("type")
                or existing.get("platform_type")
                or provider
            ).strip()
            if provider_type:
                saved["type"] = provider_type
            saved.pop("platform_type", None)
            for key, value in cfg.items():
                if key in {"name", "platform_type"}:
                    continue
                if key == "api_key":
                    api_key = str(value or "").strip()
                    if "****" in api_key:
                        continue
                    saved["api_key"] = api_key
                    continue
                saved[key] = value
            saved_providers[provider] = saved
        data["providers"] = saved_providers

        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        self._notify_provider_reload()
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

        provider_type = str(
            provider_cfg.get("type", "") or provider_cfg.get("platform_type", "") or provider_name
        ).strip()
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

                cache = ModelsDevCache(self.data_dir)
                cache.get(force_refresh=True)
                providers_data = self._load_providers_raw()
                return _json_response(
                    {
                        "success": True,
                        "changed": False,
                        "providers": providers_data,
                    }
                )

            provider_mgr = WorkspaceProviderManager(self.data_dir)
            changed = provider_mgr.refresh_models_from_dev(force=force)
            if changed:
                self._notify_provider_reload()
            # 重新加载并返回更新后的 provider 列表
            providers_data = self._load_providers_raw()
            return _json_response(
                {
                    "success": True,
                    "changed": changed,
                    "providers": providers_data,
                }
            )
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

        cache = ModelsDevCache(self.data_dir)
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
            raw_providers = self._provider_mapping_from_payload(data)
            for k, v in raw_providers.items():
                if isinstance(v, dict):
                    provider_type = str(v.get("type") or v.get("platform_type") or k).strip()
                    providers.append(
                        {
                            **v,
                            "name": k,
                            "type": provider_type,
                            "platform_type": provider_type,
                            "api_key": self._mask_api_key(v.get("api_key", "")),
                        }
                    )
            return providers
        except Exception:
            LOG.warning("读取 provider_keys 失败", exc_info=True)
            return []

    # ─── 全局 API: 可用模型列表 ───────────────────────────

    async def api_available_models_get(self, request: web.Request) -> web.Response:
        """返回全局可用模型列表（含 provider 前缀显示名）。"""
        return _json_response(build_model_catalog(self.data_dir))
