#!/usr/bin/env python
"""
CI/CD 检查脚本

该脚本执行代码质量检查、类型检查和测试，确保代码符合项目标准。
可在 CI/CD 流程中或本地开发时使用。

Go 侧车是**必需**的 LLM 传输实现（Python 侧已没有第二套），因此 Go 工具链、
侧车单测与二进制构建都是必需步骤，缺任何一项都算失败；二进制必须在 pytest
之前产出，否则依赖侧车的 E2E 用例会整组跳过，看起来却像是通过。
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SIRIUS_CHAT = PROJECT_ROOT / "sirius_pulse"
TESTS = PROJECT_ROOT / "tests"
GO_MODULE = PROJECT_ROOT / "go" / "llm-transport"
# 侧车二进制的落点必须与 sirius_pulse/utils/llm_sidecar.py 一致：
# PROJECT_ROOT / DEFAULT_BINARY_DIR / BINARY_NAME（非 Windows 没有 .exe 后缀）。
LLM_TRANSPORT_BINARY = GO_MODULE / "bin" / (
    "llm-transport.exe" if platform.system() == "Windows" else "llm-transport"
)


def run_command(cmd: list[str], description: str, *, optional: bool = False) -> bool:
    """
    运行命令并报告结果。

    Args:
        cmd: 要运行的命令
        description: 命令描述（用于输出）
        optional: 是否是可选的（失败不中止）

    Returns:
        True 如果成功，False 如果失败
    """
    print(f"\n{'='*60}")
    print(f"▶ {description}")
    print(f"{'='*60}")
    print(f"$ {' '.join(cmd)}")
    print()

    try:
        result = subprocess.run(cmd, cwd=PROJECT_ROOT)
        if result.returncode == 0:
            print(f"✓ {description} 成功\n")
            return True
        else:
            if optional:
                print(f"⚠ {description} 失败（可选，继续）\n")
                return True
            else:
                print(f"✗ {description} 失败\n")
                return False
    except Exception as e:
        print(f"✗ 运行失败: {e}\n")
        return False if not optional else True


def report_missing(description: str) -> bool:
    """
    报告一项无法执行、且不允许跳过的检查。

    Args:
        description: 缺了什么、怎么补

    Returns:
        永远为 False，便于写成 ``all_passed &= report_missing(...)``
    """
    print(f"\n✗ {description}\n")
    return False


def main() -> int:
    """主程序"""
    print("\n")
    print("╔" + "="*58 + "╗")
    print("║" + " " * 15 + "Sirius Chat CI/CD 检查" + " " * 21 + "║")
    print("╚" + "="*58 + "╝")

    all_passed = True

    # 1. 代码格式检查（必须）
    all_passed &= run_command(
        ["python", "-m", "black", "--check", str(SIRIUS_CHAT), str(TESTS)],
        "Black 格式检查",
        optional=True
    )

    # 2. Import 排序检查（必须）
    all_passed &= run_command(
        ["python", "-m", "isort", "--check-only", str(SIRIUS_CHAT), str(TESTS)],
        "isort Import 排序检查",
        optional=True
    )

    # 3. Lint 检查（可选）
    all_passed &= run_command(
        ["python", "-m", "pylint", str(SIRIUS_CHAT), "--fail-under=7.5", "--disable=C0111,W0212"],
        "pylint 代码分析",
        optional=True
    )

    # 4. 类型检查（可选）
    all_passed &= run_command(
        ["python", "-m", "mypy", str(SIRIUS_CHAT), "--ignore-missing-imports"],
        "mypy 类型检查",
        optional=True
    )

    # 5. 安全检查（可选）
    all_passed &= run_command(
        ["python", "-m", "bandit", "-r", str(SIRIUS_CHAT), "-ll"],
        "bandit 安全扫描",
        optional=True
    )

    # 6. Go 侧车（必需：Python 侧的传输实现已全部删除，没有二进制应用就起不来）
    go_binary = shutil.which("go")
    if not go_binary:
        all_passed &= report_missing(
            "未找到 go 可执行文件：请安装 Go 1.24+ 并加入 PATH"
            "（侧车是唯一的传输实现，缺工具链不能跳过）"
        )
    elif not (GO_MODULE / "go.mod").is_file():
        all_passed &= report_missing(f"未找到 Go 模块：{GO_MODULE / 'go.mod'}")
    else:
        all_passed &= run_command(
            [go_binary, "-C", str(GO_MODULE), "vet", "./..."],
            "go vet（LLM 传输侧车）",
        )
        all_passed &= run_command(
            [go_binary, "-C", str(GO_MODULE), "test", "./...", "-count=1"],
            "go test（LLM 传输侧车）",
        )
        # 二进制必须在 pytest 之前产出：找不到侧车的 E2E 用例会整组跳过，
        # 而「跳过」和「通过」在只看退出码的流水线上长得一样。
        LLM_TRANSPORT_BINARY.parent.mkdir(parents=True, exist_ok=True)
        all_passed &= run_command(
            [
                go_binary,
                "-C",
                str(GO_MODULE),
                "build",
                "-trimpath",
                "-o",
                str(LLM_TRANSPORT_BINARY),
                "./cmd/llm-transport",
            ],
            "go build（产出 LLM 传输侧车二进制）",
        )
        if LLM_TRANSPORT_BINARY.is_file():
            size_kb = LLM_TRANSPORT_BINARY.stat().st_size / 1024
            print(
                "✓ 侧车二进制已就位："
                f"{LLM_TRANSPORT_BINARY.relative_to(PROJECT_ROOT)}（{size_kb:.0f} KB）\n"
            )
        else:
            all_passed &= report_missing(f"go build 没有产出二进制：{LLM_TRANSPORT_BINARY}")

    # 7. 单元测试（必须；含需要侧车二进制的 E2E 用例）
    all_passed &= run_command(
        ["python", "-m", "pytest", "-q", "--tb=short"],
        "pytest 单元测试",
        optional=False
    )

    # 8. 测试覆盖率（可选）
    all_passed &= run_command(
        ["python", "-m", "pytest", "-q", "--cov=sirius_pulse", "--cov-report=term-missing"],
        "pytest 覆盖率报告",
        optional=True
    )

    # 组织结果
    print("\n")
    print("╔" + "="*58 + "╗")
    if all_passed:
        print("║" + " " * 18 + "✓ 所有检查通过！" + " " * 25 + "║")
    else:
        print("║" + " " * 15 + "✗ 存在失败的检查项" + " " * 24 + "║")
    print("╚" + "="*58 + "╝")
    print()

    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
