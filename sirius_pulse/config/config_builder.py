"""通用配置构建器 —— 简化配置参数的定义。

提供声明式 API，让插件和技能开发者可以轻松定义 WebUI 中的配置表单。
支持参数分组，将相关配置项聚合显示。

使用示例:
    from sirius_pulse.config import ConfigBuilder, config_param, secret

    # 方式一：使用 ConfigBuilder（支持链式分组）
    _config = ConfigBuilder()
    _config.group("认证").add("api_key", type="password", description="API 密钥", required=True)
    _config.group("模型").add("model", type="model", description="使用的模型")
    _config.group("模型").add("temperature", type="float", description="温度", default=0.7)
    parameters = _config.build()

    # 方式二：使用 config_param 标记分组
    class Config:
        api_key: str = secret("API 密钥", required=True, group="认证")
        model: str = config_param("使用的模型", type="model", group="模型")
        temperature: float = config_param("温度", default=0.7, group="模型")

    parameters = build_parameters_from_class(Config)
"""

from __future__ import annotations

import builtins
from dataclasses import dataclass
from typing import Any

# 保存内置 type 函数的引用，避免被参数名遮蔽
builtins_type = builtins.type

# Python 类型到表单类型的映射
_TYPE_MAP = {
    str: "str",
    int: "int",
    float: "number",
    bool: "boolean",
    list: "list",
}


def secret(description: str = "", **kwargs) -> Any:
    """快捷函数：定义密码/密钥类型的配置参数。

    Args:
        description: 参数描述
        **kwargs: 其他参数（required, default 等）

    Returns:
        参数标记（用于 dataclass 字段默认值）
    """
    return config_param(description, type="password", **kwargs)


@dataclass
class ParamDefinition:
    """参数定义。"""

    name: str
    type: str = "str"
    description: str = ""
    required: bool = False
    default: Any = None
    choices: list[str] | None = None
    group: str = ""
    fields: list[dict[str, Any]] | None = None  # 用于 object_array 类型，定义子字段结构


def config_param(
    description: str = "",
    *,
    type: str | type = "str",
    required: bool = False,
    default: Any = None,
    choices: list[str] | None = None,
    group: str = "",
) -> Any:
    """定义配置参数的工厂函数。

    Args:
        description: 参数描述
        type: 参数类型（str/int/float/bool/list/model 或 Python 类型）
        required: 是否必填
        default: 默认值
        choices: 可选值列表
        group: 分组名称（相同分组的参数会聚合显示）

    Returns:
        默认值（用于 dataclass 字段）
    """
    # 类型转换：如果 type 是 Python 类型对象，转换为字符串表示
    param_type = type
    if isinstance(type, builtins_type):
        param_type = _TYPE_MAP.get(type, "str")

    # 存储参数元数据，通过 __dict__ 传递给 ConfigBuilder
    return _ParamMarker(
        description=description,
        type=param_type,  # type: ignore[arg-type]
        required=required,
        default=default,
        choices=choices,
        group=group,
    )


@dataclass
class _ParamMarker:
    """参数标记，用于在 dataclass 中存储配置元数据。"""

    description: str = ""
    type: str = "str"
    required: bool = False
    default: Any = None
    choices: list[str] | None = None
    group: str = ""


class ConfigBuilder:
    """配置参数构建器。

    提供流式 API 来定义配置参数，最终生成参数列表。

    示例:
        builder = ConfigBuilder()
        builder.add("api_key", type="str", description="API 密钥", required=True, group="认证")
        builder.add("temperature", type="float", description="温度", default=0.7, group="模型")
        builder.add("model", type="model", description="模型选择", group="模型")

        parameters = builder.build()
    """

    def __init__(self) -> None:
        self._params: list[ParamDefinition] = []
        self._current_group: str = ""

    def group(self, name: str) -> "ConfigBuilder":
        """设置后续参数的分组。

        Args:
            name: 分组名称

        Returns:
            self（支持链式调用）
        """
        self._current_group = name
        return self

    def add(
        self,
        name: str,
        *,
        type: str | type = "str",
        description: str = "",
        required: bool = False,
        default: Any = None,
        choices: list[str] | None = None,
        group: str = "",
        fields: list[dict[str, Any]] | None = None,
    ) -> "ConfigBuilder":
        """添加配置参数。

        Args:
            name: 参数名
            type: 参数类型（str/int/float/bool/list/model/password/object_array/checkbox_group）
            description: 参数描述
            required: 是否必填
            default: 默认值
            choices: 可选值列表
            group: 分组名称（为空时使用 group() 设置的分组）
            fields: 子字段定义列表（仅用于 object_array 类型）
                每个字段为 {"name": str, "type": str, "description": str, "choices": list, ...}

        Returns:
            self（支持链式调用）
        """
        # 类型转换：如果 type 是 Python 类型对象，转换为字符串表示
        param_type = type
        if isinstance(type, builtins_type):
            param_type = _TYPE_MAP.get(type, "str")

        self._params.append(
            ParamDefinition(
                name=name,
                type=param_type,  # type: ignore[arg-type]
                description=description,
                required=required,
                default=default,
                choices=choices,
                group=group or self._current_group,
                fields=fields,
            )
        )
        return self

    def build(self) -> list[dict[str, Any]]:
        """构建参数列表。

        Returns:
            参数定义字典列表
        """
        return [
            {
                "name": p.name,
                "type": p.type,
                "description": p.description,
                "required": p.required,
                **({"default": p.default} if p.default is not None else {}),
                **({"choices": p.choices} if p.choices else {}),
                **({"group": p.group} if p.group else {}),
                **({"fields": p.fields} if p.fields else {}),
            }
            for p in self._params
        ]


def build_parameters_from_class(config_class: type) -> list[dict[str, Any]]:
    """从 dataclass 风格的配置类构建参数列表。

    扫描类中使用 config_param() 定义的字段，生成参数列表。

    Args:
        config_class: 配置类（使用 config_param 作为字段默认值）

    Returns:
        参数定义字典列表

    示例:
        class MyConfig:
            api_key: str = config_param("API 密钥", required=True, group="认证")
            max_results: int = config_param("最大结果数", default=10, group="通用")

        parameters = build_parameters_from_class(MyConfig)
    """
    params = []
    for name, value in vars(config_class).items():
        if name.startswith("_"):
            continue
        if isinstance(value, _ParamMarker):
            params.append(
                {
                    "name": name,
                    "type": value.type,
                    "description": value.description,
                    "required": value.required,
                    **({"default": value.default} if value.default is not None else {}),
                    **({"choices": value.choices} if value.choices else {}),
                    **({"group": value.group} if value.group else {}),
                }
            )
    return params
