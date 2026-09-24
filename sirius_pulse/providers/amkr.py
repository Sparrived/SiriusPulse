"""AMKR 连接配置。

Sirius Pulse 不再自带多供应商注册表：所有模型调用都指向同一个本地
``auto-model-key-router``（AMKR）实例，由它承担供应商与 Key 池、故障切换、
采样参数固定。本模块只解析「怎么连上 AMKR」这一件事。

配置来自 ``<global_data_path>/global_config.json``，环境变量优先：

===================  ==========================  ============================
配置键                环境变量                     默认值
===================  ==========================  ============================
``amkr_base_url``    ``SIRIUS_AMKR_BASE_URL``    ``http://127.0.0.1:8000``
``amkr_local_api_key`` ``SIRIUS_AMKR_API_KEY``   空（未配置则引擎不就绪）
``amkr_workspace``   ``SIRIUS_AMKR_WORKSPACE``   ``sirius-pulse``
``amkr_public_url``  ``SIRIUS_AMKR_PUBLIC_URL``  空（回环后端时回落同源反代 ``/amkr/``）
===================  ==========================  ============================

工作空间是本应用在**共享** AMKR 里的命名空间：多个 AI 服务共用一个 AMKR
实例时，各自持有一个空间，任务名可以重名（``cognition_analyze`` 等），
互不干扰。

空间由本框架**显式创建**（``POST /api/workspaces``），因为创建的那一刻是唯一
能同时拿到该空间两把 key 的时机——之后 AMKR 永不再返回它们。两把 key 必须当场
存下来，且只存在服务端（见 :mod:`sirius_pulse.providers.amkr_sync`）：

- **面板 key**（``amkr_ws_…``）用于把 AMKR 的工作空间面板嵌进本框架的运维页；
- **推理 key**（``amkr_ik_…``）是**模型调用**用的凭据。它被 AMKR 钉死在这一
  个工作空间上，调不了任何管理接口，因此每次对话补全都不必再动用下面那把全权
  的 ``amkr_local_api_key``。

``amkr_local_api_key`` 只用于**管理**（建空间、注册任务名）。它是能增删供应商与
Key 的管理员凭据，不该出现在推理路径上。

``amkr_public_url`` 是**浏览器**该用哪个地址访问同一个 AMKR，与 ``amkr_base_url``
（服务端容器自己怎么连）分开。容器与 AMKR 同机时后端走回环最省事，但回环地址
在用户浏览器里指向用户的机器，面板 iframe 会直接加载失败；反向代理把 AMKR 暴露
在别的域名时，两者必然不同。

两处都留空（或 ``amkr_base_url`` 就是回环）时不再回落到那个回环地址——它在浏览器里
必然指向浏览器自己的机器——而是回落到**本框架自己域名下的同源反代路径**
``/amkr``（见 :mod:`sirius_pulse.webui.amkr_proxy`）。AMKR 不发 CORS 头，面板与它
的接口必须同源，挂在 WebUI 的源上就同时满足了「浏览器可达」与「同源」。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit

from sirius_pulse.providers.base import DEFAULT_TIMEOUT_SECONDS
from sirius_pulse.utils.json_io import atomic_write_json, read_json

LOGGER = logging.getLogger(__name__)

GLOBAL_CONFIG_FILENAME = "global_config.json"

# 全局配置里存工作空间面板 key 的字段：``{工作空间名: 面板 key}``。
#
# 放这里而不是单独一个文件，是因为它天然属于「AMKR 连接」这一组配置，且
# global_config.json 已有现成的原子写。注意 ``data/`` 是 gitignored 的。
PANEL_KEYS_FIELD = "amkr_panel_keys"

# 全局配置里存工作空间**推理 key** 的字段：``{工作空间名: 推理 key}``。
#
# 与面板 key 分成两个字段而不是合成一个 ``{空间: {面板, 推理}}``：两者用途不同、
# 轮换节奏也不同（推理 key 进了模型调用路径，泄漏面更宽），分开存可以让「这个空间
# 有没有推理凭据」变成一个独立的问题——运维页据此提示哪些空间还在用全权凭据。
INFERENCE_KEYS_FIELD = "amkr_inference_keys"

# AMKR 默认监听地址（与其 README 的默认端口一致）。
AMKR_DEFAULT_BASE_URL = "http://127.0.0.1:8000"

# 本框架在**自己的源**上把 AMKR 的 WebUI 挂到哪条路径下。
#
# 定义在这里而不是反代模块里，是因为它同时被两侧依赖：反代按它注册路由，而
# ``AmkrSettings.browser_base_url`` 要用它拼出给浏览器的地址。放在底层模块可以
# 避免 providers 反向 import webui。
AMKR_PROXY_PREFIX = "/amkr"

# 本应用在 AMKR 中的默认工作空间名。
AMKR_DEFAULT_WORKSPACE = "sirius-pulse"


def _resolve_api_key(raw: str) -> str:
    """解析 API Key 的间接写法。

    支持 ``env:NAME`` 与「全大写且不含空格的名字」两种形式，均从环境变量取值；
    取不到时按字面量处理，便于在配置里直接内联。
    """
    text = raw.strip()
    if text.lower().startswith("env:"):
        return os.getenv(text[4:].strip(), "").strip()
    if text.isupper() and " " not in text:
        env_val = os.getenv(text, "").strip()
        if env_val:
            return env_val
    return text


@dataclass(slots=True)
class AmkrSettings:
    """连接一个 AMKR 实例所需的全部信息。"""

    base_url: str = AMKR_DEFAULT_BASE_URL
    api_key: str = ""
    workspace: str = AMKR_DEFAULT_WORKSPACE
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    public_url: str = ""

    @property
    def configured(self) -> bool:
        """是否已配好到可以发起请求（地址与凭据齐备）。"""
        return bool(self.base_url.strip() and self.api_key.strip())

    @property
    def browser_base_url(self) -> str:
        """浏览器该用的 AMKR 基址。

        优先级：显式配置的 ``amkr_public_url`` > 非同源的 ``amkr_base_url`` >
        本框架的同源反代路径 ``/amkr``。

        最后那条回落是关键：``amkr_base_url`` 在单机部署里通常就是宿主回环
        （``http://127.0.0.1:28881``），而回环在**用户浏览器**里指向用户自己的
        机器，面板 iframe 必然加载不出来。既然 AMKR 与 Sirius 部署在一起、浏览器
        又是先打开 Sirius 的 WebUI，那么把面板挂到 WebUI 自己的源上才是唯一
        处处可用的地址。
        """
        explicit = self.public_url.strip()
        if explicit:
            return explicit.rstrip("/")
        backend = self.base_url.strip().rstrip("/")
        if backend and not _is_loopback_url(backend):
            return backend
        return AMKR_PROXY_PREFIX


def _is_loopback_url(url: str) -> bool:
    """判断一个 URL 的主机是否是回环地址。

    只看主机名，不解析 DNS：这里要回答的是「浏览器访问它时会不会落到浏览器自己
    的机器上」，而任何名字解析都不改变 ``localhost`` 与 ``127.0.0.0/8`` 的这层
    含义。带端口的回环地址同样算——问题出在主机，不在端口。
    """
    host = urlsplit(url).hostname or ""
    if host in ("localhost", "::1"):
        return True
    if host.startswith("127."):
        return True
    return False


def load_amkr_settings(global_data_path: Path | str) -> AmkrSettings:
    """读取 AMKR 连接配置。

    配置文件缺失或损坏时不抛异常，回落到默认值——引擎随后会因为缺少凭据而
    就绪失败，那时给出的提示（去 WebUI 配置）比这里抛栈更有用。
    """
    data: dict[str, object] = {}
    path = Path(global_data_path) / GLOBAL_CONFIG_FILENAME
    try:
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = raw
    except Exception:
        LOGGER.warning("读取全局配置失败，AMKR 连接使用默认值", exc_info=True)

    base_url = (
        os.getenv("SIRIUS_AMKR_BASE_URL", "").strip()
        or str(data.get("amkr_base_url", "") or "").strip()
        or AMKR_DEFAULT_BASE_URL
    )
    raw_key = (
        os.getenv("SIRIUS_AMKR_API_KEY", "").strip()
        or str(data.get("amkr_local_api_key", "") or "").strip()
    )
    workspace = (
        os.getenv("SIRIUS_AMKR_WORKSPACE", "").strip()
        or str(data.get("amkr_workspace", "") or "").strip()
        or AMKR_DEFAULT_WORKSPACE
    )
    public_url = (
        os.getenv("SIRIUS_AMKR_PUBLIC_URL", "").strip()
        or str(data.get("amkr_public_url", "") or "").strip()
    )

    return AmkrSettings(
        base_url=base_url.rstrip("/"),
        api_key=_resolve_api_key(raw_key),
        workspace=workspace,
        public_url=public_url.rstrip("/"),
    )


# ── 工作空间凭据的存放 ────────────────────────────────────
#
# AMKR 只在**创建空间那一次**返回这两把 key，之后目录与导出都剥掉它们。因此本框架
# 必须自己存：丢了就只能去读 AMKR 的配置文件，或把空间删了重建。
#
# 存的是明文——两把都是「只对一个空间有效」的受限凭据（面板面 / 推理面），加密的
# 密钥又得再找一个地方放。真正的边界是：这些值只留在服务端，绝不出现在前端源码里。
#
# 两个字段共用下面这组读写，是为了让「怎么算一个有效的 key」只有一处定义：它们形状
# 相同，漂移了就会出现「面板 key 被认、推理 key 被当成空」这种只在某一条路径上失败
# 的怪状。


def _load_key_map(global_data_path: Path | str, field: str) -> dict[str, str]:
    """读取 ``{工作空间: key}`` 形状的字段，缺失或损坏时返回空表。"""
    data = read_json(Path(global_data_path) / GLOBAL_CONFIG_FILENAME, default=None)
    if not isinstance(data, dict):
        return {}
    raw = data.get(field)
    if not isinstance(raw, dict):
        return {}
    return {
        str(name): str(key) for name, key in raw.items() if str(name).strip() and str(key).strip()
    }


def _save_key(global_data_path: Path | str, field: str, workspace: str, key: str) -> None:
    """把一把 key 记进 ``{工作空间: key}`` 字段（保留配置里的其它字段）。"""
    path = Path(global_data_path) / GLOBAL_CONFIG_FILENAME
    data = read_json(path, default=None)
    if not isinstance(data, dict):
        data = {}
    keys = data.get(field)
    merged = dict(keys) if isinstance(keys, dict) else {}
    merged[str(workspace)] = str(key)
    data[field] = merged
    atomic_write_json(path, data)


def load_panel_keys(global_data_path: Path | str) -> dict[str, str]:
    """读取已保存的「工作空间 → 面板 key」映射。"""
    return _load_key_map(global_data_path, PANEL_KEYS_FIELD)


def save_panel_key(global_data_path: Path | str, workspace: str, key: str) -> None:
    """记下一把新拿到的面板 key。"""
    _save_key(global_data_path, PANEL_KEYS_FIELD, workspace, key)


def load_inference_keys(global_data_path: Path | str) -> dict[str, str]:
    """读取已保存的「工作空间 → 推理 key」映射。"""
    return _load_key_map(global_data_path, INFERENCE_KEYS_FIELD)


def save_inference_key(global_data_path: Path | str, workspace: str, key: str) -> None:
    """记下一把新拿到的推理 key。"""
    _save_key(global_data_path, INFERENCE_KEYS_FIELD, workspace, key)


def panel_url(ui_url: str, key: str) -> str:
    """由 AMKR WebUI 基址拼出可嵌入的工作空间面板地址。

    凭据放 **fragment**（``#k=``）而不是查询串：fragment 不会被浏览器发给服务端，
    因此既不会进 ``Referer``，也不会进 AMKR 或任何反代的访问日志。这是 AMKR 的
    硬要求（见其 ``docs/PANEL.md`` 第 4 节），不是风格选择。
    """
    base = str(ui_url or "").strip().rstrip("/")
    if not base or not str(key or "").strip():
        return ""
    return f"{base}/panel.html#k={quote(key.strip(), safe='')}"
