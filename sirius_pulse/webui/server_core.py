"""WebUI server core: WebUIServer class, routes, lifecycle, global APIs."""

from __future__ import annotations

import asyncio
import json
import logging
import multiprocessing
import socket
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from aiohttp import web

from sirius_pulse.providers.amkr import AmkrSettings, load_amkr_settings
from sirius_pulse.providers.amkr_sync import (
    AmkrError,
    collect_amkr_status_async,
    persona_panel_url,
    register_persona_tasks_async,
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
        if self._embedding_process is not None and not self._embedding_process.is_alive():
            return {
                "running": False,
                "ready": False,
                "error": self._embedding_error or "服务线程已退出",
            }
        self._embedding_ready = self._embedding_healthy()
        if self._embedding_ready:
            return {"running": True, "ready": True, "error": ""}
        if self._embedding_process is not None:
            return {"running": True, "ready": False, "error": "模型加载中..."}
        return {"running": False, "ready": False, "error": self._embedding_error or "未启动"}

    def _embedding_healthy(self) -> bool:
        import urllib.request

        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{self._embedding_port}/health", timeout=2.0
            ) as response:
                return json.loads(response.read().decode("utf-8")).get("status") == "ok"
        except Exception:
            return False

    def _start_embedding_service(self) -> None:
        if self._embedding_process is not None:
            LOG.warning("Embedding 服务已在运行")
            return

        if not self._is_port_free(self._embedding_port):
            # 端口被占用：可能是外部已启动，尝试健康检查
            if self._embedding_healthy():
                self._embedding_ready = True
                LOG.info(
                    "Embedding 服务端口 %d 已被外部进程占用且健康，跳过内部启动",
                    self._embedding_port,
                )
                return
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
        process = self._embedding_process
        if process is not None:
            LOG.info("Embedding 服务线程将随主进程退出")
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        self._embedding_process = None
        self._embedding_ready = False

    # ─── 静态页面 ─────────────────────────────────────────

    async def index(self, request: web.Request) -> web.StreamResponse:
        html_path = Path(__file__).parent / "static" / "index.html"
        if html_path.exists():
            return web.FileResponse(html_path)
        return web.Response(text="WebUI not found", status=404)

    # ─── 全局 API: 全局配置 ───────────────────────────────

    def _global_config_path(self) -> Path:
        return self.data_dir / "global_config.json"

    def _masked_global_config(self, data: dict[str, Any]) -> dict[str, Any]:
        """脱敏后返回全局配置。

        ``amkr_local_api_key`` 是 AMKR 的管理员凭据（可增删供应商与 Key），
        接口只回显掩码；前端提交掩码值时保留磁盘上的原值。
        """
        result = dict(data)
        if result.get("amkr_local_api_key"):
            result["amkr_local_api_key"] = self._mask_api_key(result["amkr_local_api_key"])
        return result

    async def api_global_config_get(self, request: web.Request) -> web.Response:
        path = self._global_config_path()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return _json_response(self._masked_global_config(data))
            except Exception:
                LOG.warning("读取全局配置失败", exc_info=True)
                pass
        return _json_response(
            {
                "webui_host": self.host,
                "webui_port": self.port,
                "log_level": "INFO",
                "amkr_base_url": "http://127.0.0.1:8000",
                "amkr_local_api_key": "",
                "amkr_workspace": "sirius-pulse",
                "amkr_ui_enabled": True,
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

        for key in (
            "webui_host",
            "webui_port",
            "log_level",
            "amkr_base_url",
            "amkr_local_api_key",
            "amkr_workspace",
            "amkr_ui_enabled",
        ):
            if key not in body:
                continue
            value = body[key]
            if key == "amkr_local_api_key":
                # 掩码值表示「保持原样」，避免前端回显后又把掩码写回磁盘。
                text = str(value or "").strip()
                if not text or self._is_masked_api_key(text):
                    continue
            data[key] = value

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
            if self.get_embedding_status()["ready"]:
                return _json_response({"success": True, "ready": True})
            if self._embedding_error:
                return _json_response({"success": False, "error": self._embedding_error})
        return _json_response({"success": False, "error": "启动超时"})

    # ─── 全局 API: 通用工具 ────────────────────────────────

    @staticmethod
    def _mask_api_key(api_key: Any) -> str:
        key = str(api_key or "").strip()
        if not key:
            return ""
        return key[:4] + "****" if len(key) > 4 else "****"

    @staticmethod
    def _is_masked_api_key(value: str) -> bool:
        """判断提交上来的 Key 是否为回显掩码（表示保持原值）。"""
        return value.endswith("****")

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

    # ─── 全局 API: AMKR 运维 ───────────────────────────────

    def _amkr_settings(self) -> AmkrSettings:
        return load_amkr_settings(self.data_dir)

    def _persona_names(self) -> list[str]:
        return [item["name"] for item in self.list_personas() if item.get("name")]

    async def api_amkr_status_get(self, request: web.Request) -> web.Response:
        """只读返回 AMKR 连接状态与各人格的任务登记情况。

        本页不做任何模型或参数编排——那是 AMKR 自带 WebUI 的职责，这里只回答
        「连得上吗」「这个名字登记了没有」，并给出跳转 AMKR 的外链。

        刻意**不**包含面板 key 或面板地址：带凭据的地址只从管理员接口
        :meth:`api_amkr_panel_get` 取，否则一次普通的只读请求就把凭据洒出去了。
        """
        status = await collect_amkr_status_async(
            self._amkr_settings(),
            self._persona_names(),
            global_data_path=self.data_dir,
        )
        return _json_response(status)

    async def api_amkr_panel_get(self, request: web.Request) -> web.Response:
        """管理员专用：返回某人格工作空间的可嵌入面板地址。

        地址的 fragment 里是明文面板 key。它必须只走管理员这一条路：钥匙只能读写
        一个空间的任务与读数，但拿到它的人可以自己调该空间的任务配置。GET 本身
        不足以挡住 viewer（中间件只拦写方法），因此这里显式判角色。
        """
        if request.get("auth_role") != "admin":
            return _json_response({"error": "权限不足，需要管理员权限"}, 403)

        persona = str(request.query.get("persona", "") or "").strip()
        if not persona:
            return _json_response({"error": "缺少 persona 参数"}, 400)
        if persona not in self._persona_names():
            return _json_response({"error": f"人格不存在: {persona}"}, 404)

        url = persona_panel_url(self._amkr_settings(), persona, self.data_dir)
        if not url:
            return _json_response(
                {
                    "error": (
                        "该人格的工作空间还没有面板 key。key 只在建空间时返回一次："
                        "请在 AMKR 里删掉该空间后重新注册，或从 AMKR 配置文件 "
                        "workspaces.<空间>.api_key 取出。"
                    )
                },
                409,
            )
        return _json_response({"persona": persona, "url": url})

    async def api_amkr_register_post(self, request: web.Request) -> web.Response:
        """为某个人格（或全部人格）建出工作空间并补齐缺失的任务名。

        建空间与注册一起做，因为顺序有依赖：面板 key 只在建空间那一次返回，必须
        当场存下来。任务只创建缺失的，已存在的一律不动，因此可以放心重复点击。
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}

        requested = str(body.get("persona", "") or "").strip()
        personas = [requested] if requested else self._persona_names()
        if not personas:
            return _json_response({"error": "没有可注册的人格"}, 400)

        settings = self._amkr_settings()
        results: dict[str, object] = {}
        for persona in personas:
            try:
                result = await register_persona_tasks_async(
                    settings,
                    persona,
                    global_data_path=self.data_dir,
                )
            except AmkrError as exc:
                results[persona] = {"error": str(exc)}
                continue
            results[persona] = result.to_dict()
        return _json_response({"results": results})

    # ─── 全局 API: 可用模型列表 ───────────────────────────

    async def api_available_models_get(self, request: web.Request) -> web.Response:
        """返回可选模型列表。

        移除多 Provider 后这里返回的是 AMKR 任务名——编排配置的 ``model``
        字段直接填任务名，由 AMKR 决定真实模型与采样参数。
        """
        return _json_response(build_model_catalog(self.data_dir))
