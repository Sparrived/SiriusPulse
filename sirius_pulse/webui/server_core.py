"""WebUI server core: WebUIServer class, routes, lifecycle, global APIs."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from aiohttp import web

from sirius_pulse.providers.amkr import (
    INFERENCE_KEYS_FIELD,
    PANEL_KEYS_FIELD,
    AmkrSettings,
    load_amkr_settings,
)
from sirius_pulse.providers.amkr_sync import (
    AmkrError,
    collect_amkr_status_async,
    persona_panel_url,
    register_persona_tasks_async,
    rotate_persona_inference_key,
)
from sirius_pulse.utils.json_io import replace_with_retry
from sirius_pulse.webui.amkr_proxy import AmkrProxy, setup_amkr_proxy_routes
from sirius_pulse.webui.app_keys import AUTH_MANAGER_KEY, DATA_DIR_KEY, WS_MANAGER_KEY
from sirius_pulse.webui.auth import AuthManager
from sirius_pulse.webui.event_bridge import EngineEventBridge
from sirius_pulse.webui.middleware import auth_middleware
from sirius_pulse.webui.model_catalog import build_model_catalog
from sirius_pulse.webui.routes import WEBUI_ROUTES
from sirius_pulse.webui.server_utils import _json_response
from sirius_pulse.webui.ws_server import WebSocketManager, WebUIFileEventBridge, setup_ws_routes

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
        # 人格引擎的实时事件（含自主回合）需要一座桥才能到达浏览器；此前
        # 事件总线没有任何订阅者，自主过程因此不可见。
        self.engine_event_bridge = EngineEventBridge(
            self.ws_manager, lambda: getattr(self, "persona_manager", None)
        )
        self.auth_manager = AuthManager(self.data_dir)
        # AMKR 同源反代：AMKR 不发 CORS 头，面板必须与它同源，因此挂在 WebUI
        # 自己的源上（运维用哪个地址打开本页，面板就在哪个源上）。
        self.amkr_proxy = AmkrProxy(self.data_dir)
        self.app = web.Application(middlewares=[auth_middleware, _no_cache_middleware])
        self.app[DATA_DIR_KEY] = self.data_dir
        self.app[AUTH_MANAGER_KEY] = self.auth_manager
        self.app[WS_MANAGER_KEY] = self.ws_manager
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None
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
        # 反代路由先于 WEBUI_ROUTES 注册：它的通配尾巴会吃掉 /amkr 下的一切，
        # 而本框架自己的接口全在 /api/ 下，两者不重叠。
        setup_amkr_proxy_routes(self.app, self.amkr_proxy)
        for spec in WEBUI_ROUTES:
            self.app.router.add_route(spec.method, spec.path, getattr(self, spec.handler_name))

    # ─── 生命周期 ─────────────────────────────────────────

    async def start(self) -> None:
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.host, self.port)
        await self.site.start()
        self.file_event_bridge.start(asyncio.get_running_loop())
        self.engine_event_bridge.start()
        LOG.info("WebUI running on http://%s:%s", self.host, self.port)

    async def stop(self) -> None:
        self.file_event_bridge.stop()
        await self.engine_event_bridge.stop()
        await self.amkr_proxy.close()
        await self.ws_manager.close_all()
        if self.site:
            await self.site.stop()
        if self.runner:
            await self.runner.cleanup()
        LOG.info("WebUI stopped")

    # ─── Embedding 状态 ────────────────────────────────────
    #
    # 向量化由 AMKR 提供，本框架不再自己拉起本地模型服务，因此这里只是**只读探测**：
    # 报告 AMKR 是否可达、配置的模型是否在 AMKR 的模型表里，以及已建索引用的是哪个
    # 模型。没有「启动/重启」动作可做——要换模型得去 AMKR 改配置。

    def get_embedding_status(self) -> dict[str, Any]:
        """返回 embedding 状态：AMKR 可达性与当前模型名。

        记忆单元的向量内联在 ``memory_units/*.json`` 里，不持久化所用模型名，
        因此这里无法再比对"已建索引的模型"，``index_stale`` 恒为 ``False``。
        """
        from sirius_pulse.embedding.client import create_embedding_client

        try:
            client = create_embedding_client(self.data_dir, self._active_persona_name)
        except Exception as exc:
            LOG.warning("构造 EmbeddingClient 失败: %s", exc)
            return {"running": False, "ready": False, "error": str(exc)}

        model = client.model
        status: dict[str, Any] = {
            "running": True,
            "ready": False,
            "error": "",
            "model": model,
            "indexed_model": "",
            "index_stale": False,
            "base_url": client._base_url,
        }
        if client.check_health():
            status["ready"] = True
            return status
        status["running"] = False
        status["error"] = f"AMKR 不可达或未配置模型 {model}（{client._base_url}）"
        return status

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

        ``amkr_panel_keys`` 与 ``amkr_inference_keys`` **整个字段都不回显**：前者是
        「工作空间 → 面板 key」，后者是「工作空间 → 推理 key」，都是明文凭据映射，
        且这个接口是任何已登录用户（含只读角色）都能读的。面板地址只从管理员专用的
        ``/api/amkr/panel`` 取；推理 key 只在轮换那一次作为响应回给管理员。这里连
        字段名都不必暴露。
        """
        result = dict(data)
        if result.get("amkr_local_api_key"):
            result["amkr_local_api_key"] = self._mask_api_key(result["amkr_local_api_key"])
        result.pop(PANEL_KEYS_FIELD, None)
        result.pop(INFERENCE_KEYS_FIELD, None)
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
                "amkr_public_url": "",
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
            "amkr_public_url",
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
        replace_with_retry(tmp, path)

        # 通知当前运行中的人格热重载全局运行时配置
        self._notify_config_reload("global")

        return _json_response({"success": True})

    # ─── 全局 API: Embedding 状态 ──────────────────────────

    async def api_embedding_status(self, request: web.Request) -> web.Response:
        return _json_response(self.get_embedding_status())

    async def api_embedding_rebuild(self, request: web.Request) -> web.Response:
        """用当前模型重建该人格的记忆单元向量索引。

        换 embedding 模型后必须做这一步：维度变了，旧向量虽然还躺在 JSON 里，但和新
        查询向量算出来的相似度没有意义。记忆单元的向量内联在 JSON 中，逐组重算。

        部分失败时如实返回 ``success: false`` 与失败数：embedding 请求超时会让一批
        向量原样留在旧维度，若还报成功，用户会以为索引已经修好，而检索结果其实是错的。
        """
        LOG.info("收到语义索引重建请求")
        loop = asyncio.get_running_loop()
        try:
            units, units_failed = await loop.run_in_executor(
                None, self._rebuild_memory_unit_embeddings
            )
        except Exception as exc:
            LOG.error("语义索引重建失败: %s", exc, exc_info=True)
            return _json_response({"success": False, "error": str(exc)})

        # 人格进程自己缓存了记忆单元向量，重建后要让 worker 丢弃缓存重新加载，
        # 否则它仍在用旧维度的向量，重建等于没做。
        if units:
            self._notify_config_reload("memory")

        failed = units_failed
        LOG.info(
            "语义索引重建完成: 记忆单元 %d 条（失败 %d）",
            units,
            units_failed,
        )
        result: dict[str, Any] = {
            "success": failed == 0,
            "units": units,
            "failed": failed,
        }
        if failed:
            result["error"] = f"有 {failed} 处向量未能重算（embedding 请求失败），" "这些条目仍与当前模型不一致，请稍后重试。"
        return _json_response(result)

    def _rebuild_memory_unit_embeddings(self) -> tuple[int, int]:
        """按当前模型重算该人格全部记忆单元的向量并落盘。

        返回 ``(涉及的单元数, 仍失败的条数)``。
        """
        from sirius_pulse.embedding.client import create_embedding_client
        from sirius_pulse.memory.units.manager import rebuild_memory_unit_embeddings
        from sirius_pulse.memory.units.store import MemoryUnitFileStore

        persona_dir = self.persona_dir
        client = create_embedding_client(self.data_dir, persona_dir.name)
        return rebuild_memory_unit_embeddings(client, MemoryUnitFileStore(persona_dir))

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

    def _notify_config_reload(self, reload_type: str, persona_dir: Path | None = None) -> None:
        """向人格写入配置重载标志，并合并快速连续请求。

        ``persona_dir`` 省略时用当前活跃人格（全局配置变更的既有语义）。按人格发出
        的变更要显式传目标目录，否则会通知错的对象——活跃人格重建了 provider，
        真正换了凭据的那个却还拿着旧 key。
        """
        try:
            flag = (persona_dir or self.persona_dir) / "engine_state" / "reload_requested"
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

        刻意**不**包含面板 key 或推理 key：前者带凭据的面板地址只从管理员接口
        :meth:`api_amkr_panel_get` 取，后者只在轮换响应里回一次，否则一次普通的
        只读请求就把凭据洒出去了。这里只报「有没有」。
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

    async def api_amkr_rotate_inference_key_post(self, request: web.Request) -> web.Response:
        """管理员专用：给某人格的工作空间换一把推理 key。

        这是「模型调用不再动用管理员凭据」这件事的补救入口：空间建于 AMKR 支持
        推理 key 之前时，本地只有面板 key，而 AMKR 不再重发——只能轮换补上。

        与面板接口同理，GET 不足以挡住 viewer（中间件只拦写方法），但轮换是**写**
        操作，中间件已按写方法要求管理员。这里仍显式判角色，避免中间件配置变动后
        悄悄放开。

        新 key 明文只在这次响应里回，且**旧 key 立即失效**。调用方必须把它存好。
        """
        if request.get("auth_role") != "admin":
            return _json_response({"error": "权限不足，需要管理员权限"}, 403)

        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}

        persona = str(body.get("persona", "") or "").strip()
        if not persona:
            return _json_response({"error": "缺少 persona 参数"}, 400)
        if persona not in self._persona_names():
            return _json_response({"error": f"人格不存在: {persona}"}, 404)

        try:
            key = rotate_persona_inference_key(
                self._amkr_settings(),
                persona,
                global_data_path=self.data_dir,
            )
        except AmkrError as exc:
            return _json_response({"error": str(exc)}, 502)

        # 正在运行的人格手里还拿着旧 key，必须让它重建 provider，否则下一次对话
        # 就是一个 401——而且要到那时才暴露。只通知这一个人格。
        self._notify_config_reload("provider", self.get_persona_dir(persona))
        return _json_response({"persona": persona, "inference_key": key})

    # ─── 全局 API: 可用模型列表 ───────────────────────────

    async def api_available_models_get(self, request: web.Request) -> web.Response:
        """返回可选模型列表。

        移除多 Provider 后这里返回的是 AMKR 任务名——编排配置的 ``model``
        字段直接填任务名，由 AMKR 决定真实模型与采样参数。
        """
        return _json_response(build_model_catalog(self.data_dir))
