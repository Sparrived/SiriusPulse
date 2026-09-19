"""向 AMKR 注册本框架使用的任务名。

Sirius Pulse 只负责**注册任务**：在自己的工作空间里建出认知任务名
（``cognition_analyze``、``memory_extract`` …）。任务指向哪个模型、用什么采样
参数，全部由运维之后在 AMKR 里配置——本模块不做任何回写。

因此这里的同步是**只创建、不修改**：AMKR 上已存在的任务一律不动，避免每次启动
都把运维调好的配置打回去。

工作空间是隐式产生的：在某个空间里建第一个任务，这个空间就存在了，所以没有
「新建工作空间」这一步。

AMKR 的配置写入走乐观并发：每次修改都要带上最新的 ``config_revision``，过期会
返回 409。因此这里串行地一个接一个提交，并把响应里的新 revision 传给下一次。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from sirius_pulse.providers.amkr import AmkrSettings

LOGGER = logging.getLogger(__name__)

# AMKR 在 revision 过期时返回的提示，用于识别「重读再试」这一种失败。
_STALE_REVISION_HINT = "配置版本已变更"


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


def register_persona_tasks(
    settings: AmkrSettings,
    persona: str,
    task_names: list[str] | None = None,
) -> SyncResult:
    """把某人格的任务名注册到 AMKR 的 ``<amkr_workspace>/<persona>`` 空间。"""
    if not settings.configured:
        raise AmkrError("尚未配置 AMKR 本地授权 Key（amkr_local_api_key）")
    with AmkrAdminClient(settings).for_workspace(workspace_for(settings, persona)) as client:
        return register_tasks(client, task_names)


async def register_persona_tasks_async(
    settings: AmkrSettings,
    persona: str,
    task_names: list[str] | None = None,
) -> SyncResult:
    """``register_persona_tasks`` 的异步版本。

    注册是十几次串行 HTTP 往返，直接放在引擎构建路径上会阻塞事件循环长达数秒
    （AMKR 不可达时更久）。因此丢到线程里执行。
    """
    return await asyncio.to_thread(register_persona_tasks, settings, persona, task_names)


# ── 只读巡检（WebUI 运维页） ───────────────────────────────


@dataclass(slots=True)
class WorkspaceState:
    """某人格在 AMKR 上的任务登记状态。"""

    persona: str
    workspace: str
    registered: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    error: str = ""

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
        }


def amkr_ui_url(settings: AmkrSettings, health: dict[str, Any] | None = None) -> str:
    """AMKR 自带 WebUI 的地址，供运维页「打开 AMKR」外链使用。

    AMKR 未被嵌入其它服务时挂载前缀为空，``/health`` 的 ``webui_path`` 就是
    ``/ui``；未挂载时该字段为 null，此时退回 ``/ui``——让运维至少能点进一个
    明确的位置，而不是一个被拼坏的地址。
    """
    if not settings.base_url:
        return ""
    path = ""
    if isinstance(health, dict):
        raw = health.get("webui_path")
        if isinstance(raw, str) and raw.strip():
            path = raw.strip()
    return f"{settings.base_url.rstrip('/')}{path or '/ui'}"


def inspect_persona_workspace(
    settings: AmkrSettings,
    persona: str,
    task_names: list[str] | None = None,
) -> WorkspaceState:
    """读取某人格工作空间里已登记与缺失的任务名。"""
    wanted = list(task_names) if task_names is not None else known_task_names()
    workspace = workspace_for(settings, persona)
    state = WorkspaceState(persona=persona, workspace=workspace)
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
) -> dict[str, Any]:
    """汇总 AMKR 连接状态与各人格的任务登记情况。

    只读：不会创建或修改任何任务，可安全地反复调用。
    """
    wanted = list(task_names) if task_names is not None else known_task_names()
    status: dict[str, Any] = {
        "configured": settings.configured,
        "base_url": settings.base_url,
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

    status["workspaces"] = [
        inspect_persona_workspace(settings, persona, wanted).to_dict()
        for persona in (personas or [])
    ]
    return status


async def collect_amkr_status_async(
    settings: AmkrSettings,
    personas: list[str] | None = None,
    task_names: list[str] | None = None,
) -> dict[str, Any]:
    """``collect_amkr_status`` 的异步版本：探测会串行往返多次，放到线程里跑。"""
    return await asyncio.to_thread(collect_amkr_status, settings, personas, task_names)
