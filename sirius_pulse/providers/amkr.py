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
===================  ==========================  ============================

工作空间是本应用在**共享** AMKR 里的命名空间：多个 AI 服务共用一个 AMKR
实例时，各自持有一个空间，任务名可以重名（``cognition_analyze`` 等），
互不干扰。

空间由本框架**显式创建**（``POST /api/workspaces``），因为创建的那一刻是唯一
能拿到该空间「面板 key」的时机——之后 AMKR 永不再返回它。这把 key 用于把
AMKR 的工作空间面板嵌进本框架的运维页，因此必须当场存下来，且只存在服务端
（见 :mod:`sirius_pulse.providers.amkr_sync`）。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from sirius_pulse.providers.base import DEFAULT_TIMEOUT_SECONDS
from sirius_pulse.utils.json_io import atomic_write_json, read_json

LOGGER = logging.getLogger(__name__)

GLOBAL_CONFIG_FILENAME = "global_config.json"

# 全局配置里存工作空间面板 key 的字段：``{工作空间名: 面板 key}``。
#
# 放这里而不是单独一个文件，是因为它天然属于「AMKR 连接」这一组配置，且
# global_config.json 已有现成的原子写。注意 ``data/`` 是 gitignored 的。
PANEL_KEYS_FIELD = "amkr_panel_keys"

# AMKR 默认监听地址（与其 README 的默认端口一致）。
AMKR_DEFAULT_BASE_URL = "http://127.0.0.1:8000"

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

    @property
    def configured(self) -> bool:
        """是否已配好到可以发起请求（地址与凭据齐备）。"""
        return bool(self.base_url.strip() and self.api_key.strip())


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

    return AmkrSettings(
        base_url=base_url.rstrip("/"),
        api_key=_resolve_api_key(raw_key),
        workspace=workspace,
    )


# ── 工作空间面板 key 的存放 ────────────────────────────────
#
# AMKR 只在**创建空间那一次**返回面板 key，之后目录与导出都剥掉它。因此本框架
# 必须自己存：丢了就只能去读 AMKR 的配置文件，或把空间删了重建。
#
# 存的是明文——它是一把「只能读写一个空间的任务与读数」的受限凭据，加密的密钥
# 又得再找一个地方放。真正的边界是：这些值只留在服务端，绝不出现在前端源码里。


def load_panel_keys(global_data_path: Path | str) -> dict[str, str]:
    """读取已保存的「工作空间 → 面板 key」映射。"""
    data = read_json(Path(global_data_path) / GLOBAL_CONFIG_FILENAME, default=None)
    if not isinstance(data, dict):
        return {}
    raw = data.get(PANEL_KEYS_FIELD)
    if not isinstance(raw, dict):
        return {}
    return {
        str(name): str(key) for name, key in raw.items() if str(name).strip() and str(key).strip()
    }


def save_panel_key(global_data_path: Path | str, workspace: str, key: str) -> None:
    """记下一把新拿到的面板 key（保留配置里的其它字段）。"""
    path = Path(global_data_path) / GLOBAL_CONFIG_FILENAME
    data = read_json(path, default=None)
    if not isinstance(data, dict):
        data = {}
    keys = data.get(PANEL_KEYS_FIELD)
    merged = dict(keys) if isinstance(keys, dict) else {}
    merged[str(workspace)] = str(key)
    data[PANEL_KEYS_FIELD] = merged
    atomic_write_json(path, data)


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
