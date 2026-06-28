#!/usr/bin/env python
"""
本地开发环境初始化脚本

该脚本自动初始化开发环境，包括：
1. 安装所有必要的依赖
2. 安装 pre-commit 钩子
3. 运行初始代码检查
4. 输出开发指南
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent


def run_command(cmd: list[str] | str, *, check: bool = True) -> int:
    """运行命令"""
    if isinstance(cmd, str):
        cmd = cmd.split()
    print(f"$ {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=PROJECT_ROOT)
    if check and result.returncode != 0:
        print(f"错误: 命令失败")
        return result.returncode
    return result.returncode


def main() -> int:
    """主程序"""
    print("\n")
    print("╔" + "="*58 + "╗")
    print("║" + " " * 12 + "Sirius Chat 本地开发环境初始化" + " " * 14 + "║")
    print("╚" + "="*58 + "╝")

    print("\n▶ 步骤 1: 安装项目及所有开发依赖...")
    if run_command([sys.executable, "-m", "pip", "install", "-e", ".[dev,test]"], check=False) != 0:
        print("⚠ pip 安装失败，请检查网络连接")
        return 1
    print("✓ 依赖安装完成\n")

    print("▶ 步骤 2: 安装 pre-commit 钩子...")
    if run_command(["pre-commit", "install"], check=False) != 0:
        print("⚠ pre-commit 安装失败")
        return 1
    print("✓ Pre-commit 钩子已安装\n")

    print("▶ 步骤 3: 运行初始代码检查...")
    if run_command([sys.executable, "-m", "pytest", "-q"], check=False) != 0:
        print("⚠ 初始测试失败")
        return 1
    print("✓ 所有测试通过\n")

    print("╔" + "="*58 + "╗")
    print("║" + " " * 18 + "✓ 环境初始化完成！" + " " * 22 + "║")
    print("╚" + "="*58 + "╝")

    print("\n📚 开发快速指南:")
    print("-" * 60)
    print("  编写代码:")
    print("    - 遵循 Python 3.12+ 最佳实践")
    print("    - 使用类型注解")
    print("    - 编写对应的测试")
    print()
    print("  代码审查:")
    print("    make format        # 格式化代码（black + isort）")
    print("    make lint          # 运行 linters")
    print("    make typecheck     # 类型检查")
    print("    make test          # 运行测试")
    print("    make test-cov      # 生成覆盖率报告")
    print()
    print("  提交代码:")
    print("    - pre-commit 钩子会在 git commit 前自动运行")
    print("    - 遵循 Conventional Commits 规范:")
    print("      feat(module): description")
    print("      fix(module): description")
    print("      docs(file): description")
    print()
    print("  CI/CD:")
    print("    - Push 后自动在 GitHub 上运行测试")
    print("    - 检查 .github/workflows/ci.yml 了解详情")
    print("-" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
