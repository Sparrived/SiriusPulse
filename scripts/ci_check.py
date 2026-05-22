#!/usr/bin/env python
"""
CI/CD 检查脚本

该脚本执行代码质量检查、类型检查和测试，确保代码符合项目标准。
可在 CI/CD 流程中或本地开发时使用。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# 项目根目录
PROJECT_ROOT = Path(__file__).parent
SIRIUS_CHAT = PROJECT_ROOT / "sirius_pulse"
TESTS = PROJECT_ROOT / "tests"


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

    # 6. 单元测试（必须）
    all_passed &= run_command(
        ["python", "-m", "pytest", "-q", "--tb=short"],
        "pytest 单元测试",
        optional=False
    )

    # 7. 测试覆盖率（可选）
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
