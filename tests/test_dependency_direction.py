"""依赖方向：领域与工具不依赖引擎内核。

- ``tools/``（含内置工具）通过注入的引擎上下文访问引擎，不直接 import ``core``；
- ``life/`` 是她自己生活的纯领域逻辑，既不依赖 ``core`` 也不依赖 ``tools``；
- 被双方共用的小模块（交互标记、prompt 排列约定）同样不依赖 ``core``。

这里扫的是源码里所有 import（包括函数内的延迟 import），不只看文件顶部。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "sirius_pulse"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
            # ``from sirius_pulse import core`` 也算。
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def _violations(paths: list[Path], forbidden: tuple[str, ...]) -> list[str]:
    found = []
    for path in paths:
        for module in sorted(_imports(path)):
            if any(module == prefix or module.startswith(prefix + ".") for prefix in forbidden):
                found.append(f"{path.relative_to(PACKAGE)} -> {module}")
    return found


@pytest.mark.parametrize(
    ("area", "forbidden"),
    [
        ("tools", ("sirius_pulse.core",)),
        ("life", ("sirius_pulse.core", "sirius_pulse.tools")),
    ],
)
def test_domain_packages_do_not_reach_into_the_engine(area: str, forbidden: tuple[str, ...]):
    paths = sorted((PACKAGE / area).rglob("*.py"))
    assert paths

    assert _violations(paths, forbidden) == []


@pytest.mark.parametrize(
    "module",
    ["adapters/interaction_markers.py", "utils/prompt_layout.py"],
)
def test_shared_helpers_stay_engine_free(module: str):
    assert _violations([PACKAGE / module], ("sirius_pulse.core",)) == []
