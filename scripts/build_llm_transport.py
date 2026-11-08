#!/usr/bin/env python
"""构建 LLM 传输侧车（Go）二进制。

侧车是**必需**的：Python 侧已经没有任何传输实现（``OpenAICompatibleProvider``
已删除），运行前必须先跑本脚本产出二进制，否则应用不就绪：

    python scripts/build_llm_transport.py            # 构建到 go/llm-transport/bin/
    python scripts/build_llm_transport.py --test     # 先跑 go test 再构建
    python scripts/build_llm_transport.py --output D:/tools/llm-transport.exe

二进制默认不提交（见 .gitignore）；Python 侧按
``配置 llm_transport_binary`` → 环境变量 ``SIRIUS_LLM_TRANSPORT_BIN`` →
``go/llm-transport/bin/`` → ``PATH`` 的顺序查找。
"""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GO_MODULE = PROJECT_ROOT / "go" / "llm-transport"
BIN_DIR = GO_MODULE / "bin"


def _binary_name() -> str:
    return "llm-transport.exe" if platform.system() == "Windows" else "llm-transport"


def _require_go() -> str:
    go = shutil.which("go")
    if not go:
        raise SystemExit("未找到 go 可执行文件：请先安装 Go 1.24+ 并加入 PATH")
    return go


def _run(go: str, args: list[str], *, description: str) -> None:
    print(f"$ go {' '.join(args)}")
    completed = subprocess.run([go, *args], cwd=str(GO_MODULE))
    if completed.returncode != 0:
        raise SystemExit(f"{description} 失败（exit={completed.returncode}）")


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认 GBK，构建成功后的 “✓” 会抛 UnicodeEncodeError（构建本身没失败）。
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="构建 LLM 传输侧车（Go）")
    parser.add_argument(
        "--output",
        "-o",
        default="",
        help="输出路径；默认 go/llm-transport/bin/llm-transport[.exe]",
    )
    parser.add_argument("--test", action="store_true", help="构建前先跑 go vet + go test")
    parser.add_argument("--clean", action="store_true", help="构建前清空 bin/")
    args = parser.parse_args(argv)

    if not (GO_MODULE / "go.mod").is_file():
        raise SystemExit(f"未找到 Go 模块：{GO_MODULE / 'go.mod'}")

    go = _require_go()

    if args.clean and BIN_DIR.exists():
        for item in BIN_DIR.iterdir():
            if item.is_file():
                item.unlink()

    if args.test:
        _run(go, ["vet", "./..."], description="go vet")
        _run(go, ["test", "./...", "-count=1"], description="go test")

    output = Path(args.output).expanduser() if args.output else BIN_DIR / _binary_name()
    output.parent.mkdir(parents=True, exist_ok=True)

    _run(
        go,
        ["build", "-trimpath", "-o", str(output), "./cmd/llm-transport"],
        description="go build",
    )

    size_kb = output.stat().st_size / 1024
    print(f"\n✓ 已构建：{output}（{size_kb:.0f} KB）")
    print("二进制不入库：换机器或清理工作区后需要重新构建。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
