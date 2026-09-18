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
互不干扰。空间由「在里面建第一个任务」隐式产生，不需要预先创建。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from sirius_pulse.providers.base import DEFAULT_TIMEOUT_SECONDS

LOGGER = logging.getLogger(__name__)

GLOBAL_CONFIG_FILENAME = "global_config.json"

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
