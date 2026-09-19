"""在 AMKR 里建出本框架的工作空间与任务名。

Sirius Pulse 只负责**建出容器**：为每个人格建一个工作空间，再往里注册认知任务名
（``cognition_analyze``、``memory_extract`` …）。任务指向哪个模型、用什么采样
参数，全部由运维之后在 AMKR 里配置——本模块不做任何回写。

因此任务同步是**只创建、不修改**：AMKR 上已存在的任务一律不动，避免每次启动
都把运维调好的配置打回去。

工作空间**显式创建**（``POST /api/workspaces``）而不是靠「建第一个任务」隐式产生。
原因是创建的那一刻是拿到该空间**面板 key** 的唯一时机：AMKR 之后不再以任何接口
返回它（目录与导出都刻意剥掉）。有了这把 key，运维页才能把 AMKR 的工作空间面板
嵌进来，让运营自己看用量、自己调这个空间的任务。key 存在全局配置的
``amkr_panel_keys`` 字段里，与 ``amkr_local_api_key`` 一样只在服务端。

AMKR 的配置写入走乐观并发：每次修改都要带上最新的 ``config_revision``，过期会
返回 409。因此这里串行地一个接一个提交，并把响应里的新 revision 传给下一次。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from sirius_pulse.providers.amkr import (
    AmkrSettings,
    load_panel_keys,
    panel_url,
    save_panel_key,
)

LOGGER = logging.getLogger(__name__)

# AMKR 在 revision 过期时的返回提示，用于识别「重读再试」这一种失败。
_STALE_REVISION_HINT = "配置版本已变更"

# AMKR 在空间已存在时的返回提示（409）。
_WORKSPACE_EXISTS_HINT = "工作空间已存在"


class AmkrError(RuntimeError):
    """调用 AMKR 管理接口失败。"""


@dataclass(slots=True)
class SyncResult:
    """一次注册的结果，供 WebUI 展示。"""

    created: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.failed

    def to_dict(self) -> dict[str, object]:
        return {
            "created": self.created,
            "existing": self.existing,
            "failed": self.failed,
        }


class AmkrAdminClient:
    """AMKR 管理接口的最小客户端。

    只覆盖任务注册需要的读写，够用且不必跟随 AMKR 的接口扩张。

    注册一轮要发十几次请求，因此连接在上下文期间复用（``with`` / ``close``），
    而不是每次请求都重新握手。
    """

    def __init__(self, settings: AmkrSettings, *, timeout_seconds: float = 15.0) -> None:
        self._settings = settings
        self._timeout = timeout_seconds
        self._workspace = settings.workspace
        self._client: httpx.Client | None = None

    def __enter__(self) -> AmkrAdminClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    @property
    def settings(self) -> AmkrSettings:
        return self._settings

    def for_workspace(self, workspace: str) -> AmkrAdminClient:
        """返回一个改绑到指定工作空间的客户端（其余设置不变，连接独立）。"""
        clone = AmkrAdminClient(self._settings, timeout_seconds=self._timeout)
        clone._workspace = workspace
        return clone

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def _headers(self) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._settings.api_key}"}
        if self._workspace:
            headers["X-AMKR-Workspace"] = self._workspace
        return headers

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        try:
            body = response.json()
        except Exception:
            return response.text.strip() or f"HTTP {response.status_code}"
        if isinstance(body, dict):
            for key in ("error", "message", "detail"):
                value = body.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return response.text.strip() or f"HTTP {response.status_code}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, object] | None = None,
    ) -> httpx.Response:
        try:
            return self._http().request(
                method,
                f"{self._settings.base_url}{path}",
                headers=self._headers(),
                json=json_body,
            )
        except httpx.HTTPError as exc:
            raise AmkrError(f"无法连接 AMKR（{self._settings.base_url}）：{exc}") from exc

    # ── 读取 ──────────────────────────────────────────────

    def health(self) -> dict[str, Any]:
        """读取 AMKR 健康状态，同时用作连通性探测。"""
        response = self._request("GET", "/health")
        if response.status_code != 200:
            raise AmkrError(self._error_message(response))
        try:
            data = response.json()
        except Exception as exc:
            raise AmkrError(f"AMKR /health 返回的不是 JSON：{exc}") from exc
        return data if isinstance(data, dict) else {}

    def list_tasks(self) -> tuple[list[dict[str, Any]], str | None]:
        """列出本工作空间的任务，同时返回当前 config_revision。"""
        response = self._request("GET", "/api/tasks")
        if response.status_code == 401:
            raise AmkrError("AMKR 拒绝了本地授权 Key（401），请检查 amkr_local_api_key")
        if response.status_code != 200:
            raise AmkrError(self._error_message(response))
        try:
            data = response.json()
        except Exception as exc:
            raise AmkrError(f"AMKR 任务列表不是合法 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise AmkrError("AMKR 任务列表格式异常")
        raw_tasks = data.get("tasks")
        tasks = (
            [item for item in raw_tasks if isinstance(item, dict)]
            if isinstance(raw_tasks, list)
            else []
        )
        revision = data.get("config_revision")
        return tasks, str(revision) if isinstance(revision, str) and revision else None

    def list_workspaces(self) -> list[dict[str, Any]]:
        """列出 AMKR 上全部有任务的工作空间。"""
        response = self._request("GET", "/api/workspaces")
        if response.status_code != 200:
            raise AmkrError(self._error_message(response))
        try:
            data = response.json()
        except Exception as exc:
            raise AmkrError(f"AMKR 工作空间列表不是合法 JSON：{exc}") from exc
        if not isinstance(data, dict):
            return []
        raw = data.get("workspaces")
        return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []

    # ── 写入 ──────────────────────────────────────────────

    def create_task(self, name: str, revision: str | None) -> str | None:
        """注册一个任务名，返回新的 config_revision。

        只提交名字：模型与参数留给 AMKR 侧配置，本框架不预设。
        """
        payload: dict[str, object] = {"name": name, "config_revision": revision}
        response = self._request("POST", "/api/tasks", json_body=payload)
        if response.status_code in (200, 201):
            return self._revision_of(response)
        raise AmkrError(self._error_message(response))

    def create_workspace(self, name: str, revision: str | None) -> str:
        """显式建出一个工作空间，返回它的面板 key。

        key 由 AMKR 生成（不传 ``api_key``）：本框架没有既定的凭据命名规范，
        让服务端生成可以少一处自造格式。**响应是拿到它的唯一时机**，调用方必须
        立刻存下来。

        ``revision`` 为 ``None`` 时先读一次任务列表取。空间已存在时 AMKR 返回
        409（它不会返回旧 key），这里包成带提示的 :class:`AmkrError`。
        """
        if revision is None:
            _, revision = self.list_tasks()
        payload: dict[str, object] = {"name": name, "config_revision": revision}
        response = self._request("POST", "/api/workspaces", json_body=payload)
        if response.status_code not in (200, 201):
            message = self._error_message(response)
            if response.status_code == 409 and _WORKSPACE_EXISTS_HINT in message:
                raise AmkrError(
                    f"{message}。AMKR 不会再返回它的面板 key：请从 AMKR 配置文件 "
                    f'workspaces."{name}".api_key 取出，或在 AMKR 里删掉该空间后重新注册。'
                )
            raise AmkrError(message)
        try:
            data = response.json()
        except Exception as exc:
            raise AmkrError(f"AMKR 建空间的响应不是 JSON：{exc}") from exc
        key = str(data.get("api_key", "") or "").strip() if isinstance(data, dict) else ""
        if not key:
            raise AmkrError("AMKR 建空间成功但没有返回面板 key")
        return key

    def delete_task(self, name: str, revision: str | None) -> str | None:
        """删除任务，返回新的 config_revision。"""
        payload: dict[str, object] = {"config_revision": revision}
        response = self._request("DELETE", f"/api/tasks/{quote(name, safe='')}", json_body=payload)
        if response.status_code in (200, 204):
            return self._revision_of(response)
        raise AmkrError(self._error_message(response))

    @staticmethod
    def _revision_of(response: httpx.Response) -> str | None:
        try:
            data = response.json()
        except Exception:
            return None
        if isinstance(data, dict):
            revision = data.get("config_revision")
            if isinstance(revision, str) and revision:
                return revision
        return None

    @staticmethod
    def is_stale_revision(exc: Exception) -> bool:
        return _STALE_REVISION_HINT in str(exc)


def workspace_for(settings: AmkrSettings, persona: str) -> str:
    """本框架给某个人格使用的工作空间名。

    AMKR 里任务名只在工作空间内唯一，而模型编排是**人格级**配置，因此一个人格
    对应一个子空间：``<amkr_workspace>/<persona>``。这样各人格在 AMKR 里各自
    把同名任务指向不同模型，互不干扰。
    """
    persona_name = str(persona).strip()
    base = settings.workspace.strip() or "sirius-pulse"
    return f"{base}/{persona_name}" if persona_name else base


def known_task_names() -> list[str]:
    """本框架需要注册到 AMKR 的全部任务名。"""
    from sirius_pulse.core.model_router import _DEFAULT_TASK_REGISTRY

    return list(_DEFAULT_TASK_REGISTRY)


def register_tasks(
    client: AmkrAdminClient,
    task_names: list[str] | None = None,
) -> SyncResult:
    """在客户端绑定的工作空间里注册缺失的任务名。

    已存在的任务只记入 ``existing``，不比对、不更新——参数与模型的调整权在 AMKR。
    """
    result = SyncResult()
    try:
        existing, revision = client.list_tasks()
    except AmkrError as exc:
        raise AmkrError(str(exc)) from exc

    present = {
        str(item.get("name", "")).strip() for item in existing if str(item.get("name", "")).strip()
    }

    for name in task_names if task_names is not None else known_task_names():
        if name in present:
            result.existing.append(name)
            continue
        revision = _create_with_retry(client, name, revision, result)

    return result


def _create_with_retry(
    client: AmkrAdminClient,
    name: str,
    revision: str | None,
    result: SyncResult,
) -> str | None:
    try:
        new_revision = client.create_task(name, revision)
    except AmkrError as exc:
        if not AmkrAdminClient.is_stale_revision(exc):
            result.failed[name] = str(exc)
            return revision
        # 版本过期说明期间有人改过配置，重读后重试一次。
        try:
            _, revision = client.list_tasks()
            new_revision = client.create_task(name, revision)
        except AmkrError as retry_exc:
            result.failed[name] = str(retry_exc)
            return revision
    result.created.append(name)
    return new_revision or revision


def ensure_persona_workspace_key(
    settings: AmkrSettings,
    persona: str,
    global_data_path: Path | str,
) -> str:
    """确保某人格的工作空间存在，并返回它的面板 key。

    AMKR 只在创建空间那一次返回 key，所以顺序不能反：**先建空间拿 key，再注册
    任务**。已存过 key 的空间直接返回，不再打扰 AMKR（重复创建会被 409 拒掉）。

    空间已存在但本地没有 key 时抛 :class:`AmkrError`——AMKR 不会重发 key，只能
    去读它的配置文件或删掉重建。这是必须让运维看见的状况，不能静默跳过。
    """
    if not settings.configured:
        raise AmkrError("尚未配置 AMKR 本地授权 Key（amkr_local_api_key）")

    workspace = workspace_for(settings, persona)
    existing = load_panel_keys(global_data_path).get(workspace)
    if existing:
        return existing

    with AmkrAdminClient(settings).for_workspace(workspace) as client:
        key = client.create_workspace(workspace, None)

    save_panel_key(global_data_path, workspace, key)
    LOGGER.info("已在 AMKR 建出工作空间 %s 并保存面板 key", workspace)
    return key


def register_persona_tasks(
    settings: AmkrSettings,
    persona: str,
    task_names: list[str] | None = None,
    *,
    global_data_path: Path | str | None = None,
) -> SyncResult:
    """把某人格的任务名注册到 AMKR 的 ``<amkr_workspace>/<persona>`` 空间。

    给了 ``global_data_path`` 就先确保空间存在并持有它的面板 key（见
    :func:`ensure_persona_workspace_key`）——正常路径都会给；只有针对已有空间的
    纯注册（测试、补登记）才省略。
    """
    if not settings.configured:
        raise AmkrError("尚未配置 AMKR 本地授权 Key（amkr_local_api_key）")
    if global_data_path is not None:
        ensure_persona_workspace_key(settings, persona, global_data_path)
    with AmkrAdminClient(settings).for_workspace(workspace_for(settings, persona)) as client:
        return register_tasks(client, task_names)


async def register_persona_tasks_async(
    settings: AmkrSettings,
    persona: str,
    task_names: list[str] | None = None,
    *,
    global_data_path: Path | str | None = None,
) -> SyncResult:
    """``register_persona_tasks`` 的异步版本。

    注册是十几次串行 HTTP 往返，直接放在引擎构建路径上会阻塞事件循环长达数秒
    （AMKR 不可达时更久）。因此丢到线程里执行。
    """
    return await asyncio.to_thread(
        register_persona_tasks, settings, persona, task_names, global_data_path=global_data_path
    )


# ── 只读巡检（WebUI 运维页） ───────────────────────────────


@dataclass(slots=True)
class WorkspaceState:
    """某人格在 AMKR 上的任务登记状态。"""

    persona: str
    workspace: str
    registered: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    error: str = ""
    panel_ready: bool = False

    @property
    def ok(self) -> bool:
        return not self.error

    def to_dict(self) -> dict[str, Any]:
        return {
            "persona": self.persona,
            "workspace": self.workspace,
            "registered": self.registered,
            "missing": self.missing,
            "error": self.error,
            "panel_ready": self.panel_ready,
        }


def amkr_ui_url(settings: AmkrSettings, health: dict[str, Any] | None = None) -> str:
    """AMKR 自带 WebUI 的地址，供运维页「打开 AMKR」外链使用。

    AMKR 未被嵌入其它服务时挂载前缀为空，``/health`` 的 ``webui_path`` 就是
    ``/ui``；未挂载时该字段为 null，此时退回 ``/ui``——让运维至少能点进一个
    明确的位置，而不是一个被拼坏的地址。

    用 ``browser_base_url`` 而不是 ``base_url``：这个地址是给**用户的浏览器**点
    的。部署在同一台机器上时后端走回环最省事，但那在用户浏览器里指向用户自己
    的机器，链接必然打不开。
    """
    base = settings.browser_base_url
    if not base:
        return ""
    path = ""
    if isinstance(health, dict):
        raw = health.get("webui_path")
        if isinstance(raw, str) and raw.strip():
            path = raw.strip()
    return f"{base}{path or '/ui'}"


def inspect_persona_workspace(
    settings: AmkrSettings,
    persona: str,
    task_names: list[str] | None = None,
    *,
    global_data_path: Path | str | None = None,
) -> WorkspaceState:
    """读取某人格工作空间里已登记与缺失的任务名。

    ``global_data_path`` 只用来判断本地是否存有该空间的面板 key（``panel_ready``），
    不会向 AMKR 索取任何凭据——key 早已拿不到了。
    """
    wanted = list(task_names) if task_names is not None else known_task_names()
    workspace = workspace_for(settings, persona)
    state = WorkspaceState(persona=persona, workspace=workspace)
    if global_data_path is not None:
        state.panel_ready = bool(load_panel_keys(global_data_path).get(workspace))
    try:
        with AmkrAdminClient(settings).for_workspace(workspace) as client:
            tasks, _ = client.list_tasks()
    except AmkrError as exc:
        state.error = str(exc)
        state.missing = list(wanted)
        return state
    present = {
        str(item.get("name", "")).strip() for item in tasks if str(item.get("name", "")).strip()
    }
    state.registered = [name for name in wanted if name in present]
    state.missing = [name for name in wanted if name not in present]
    return state


def collect_amkr_status(
    settings: AmkrSettings,
    personas: list[str] | None = None,
    task_names: list[str] | None = None,
    *,
    global_data_path: Path | str | None = None,
) -> dict[str, Any]:
    """汇总 AMKR 连接状态与各人格的任务登记情况。

    只读：不会创建或修改任何任务，可安全地反复调用。
    """
    wanted = list(task_names) if task_names is not None else known_task_names()
    status: dict[str, Any] = {
        "configured": settings.configured,
        "base_url": settings.base_url,
        # 浏览器侧的地址，可能因反向代理而与 base_url 不同（见 AmkrSettings）。
        "browser_base_url": settings.browser_base_url,
        "workspace_base": settings.workspace,
        "ui_url": amkr_ui_url(settings),
        "reachable": False,
        "error": "",
        "version": "",
        "ops_enabled": False,
        "webui_mounted": False,
        "known_tasks": wanted,
        "workspaces": [],
    }
    if not settings.configured:
        status["error"] = "尚未配置 AMKR 本地授权 Key（amkr_local_api_key）"
        return status

    # 逐人格的巡检即使在 AMKR 不可达时也要跑：它同时回答「这个空间有没有面板
    # key」，而那是本地事实——连不上不该让运维以为面板也没配好。
    status["workspaces"] = [
        inspect_persona_workspace(
            settings, persona, wanted, global_data_path=global_data_path
        ).to_dict()
        for persona in (personas or [])
    ]

    with AmkrAdminClient(settings) as client:
        try:
            health = client.health()
        except AmkrError as exc:
            status["error"] = str(exc)
            return status
        status["reachable"] = True
        status["version"] = str(health.get("version", "") or "")
        status["ops_enabled"] = bool(health.get("ops_enabled"))
        status["webui_mounted"] = bool(health.get("webui_mounted"))
        status["ui_url"] = amkr_ui_url(settings, health)

    return status


async def collect_amkr_status_async(
    settings: AmkrSettings,
    personas: list[str] | None = None,
    task_names: list[str] | None = None,
    *,
    global_data_path: Path | str | None = None,
) -> dict[str, Any]:
    """``collect_amkr_status`` 的异步版本：探测会串行往返多次，放到线程里跑。"""
    return await asyncio.to_thread(
        collect_amkr_status,
        settings,
        personas,
        task_names,
        global_data_path=global_data_path,
    )


def persona_panel_url(
    settings: AmkrSettings,
    persona: str,
    global_data_path: Path | str,
) -> str:
    """某人格工作空间的**可嵌入面板地址**（含明文 key）。

    这是唯一会把 key 交给浏览器的出口，因此调用方必须把它挡在管理员之下：地址里
    带凭据，任何能看到它的人都能读写那个空间的任务。
    """
    workspace = workspace_for(settings, persona)
    key = load_panel_keys(global_data_path).get(workspace, "")
    if not key:
        return ""
    return panel_url(amkr_ui_url(settings), key)
