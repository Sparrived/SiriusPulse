"""聊天图片的本地路径解析：只暴露框架自己能直接复用的本地文件。

聊天记录里的图片既可能是本地缓存文件，也可能是平台签名 URL 或 data URL。模型想把
「刚收到的那张图」原样交给 ``image_generate`` 当参考图时，只有本地文件是稳定可读的：
平台 URL 会过期，data URL 不是文件路径。本模块统一这层判定，聊天历史 XML 与当轮消息
注入共用，避免两处口径漂移。
"""

from __future__ import annotations


def resolve_local_image_path(value: object) -> str:
    """返回图片输入的本地绝对路径；不是现存本地文件时返回空串。

    ``file://`` URI 会还原成路径，``~`` 会展开；http(s) 与 data 地址一律视为不可复用。
    文件已被缓存清理掉时同样返回空串，调用方就不会给模型一个读不到的路径。
    """
    # 延迟导入：providers.base 会 import 本包的其他模块，放模块顶层容易成环。
    from sirius_pulse.providers.base import _resolve_local_file_reference

    resolved = _resolve_local_file_reference(str(value or "").strip())
    return str(resolved) if resolved is not None else ""
