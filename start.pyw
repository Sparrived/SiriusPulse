"""无窗口启动器 — 双击运行不会出现 CMD 窗口。

使用 pythonw.exe 执行，所有输出写入日志文件。
"""
import os
import sys
from pathlib import Path

# 确保工作目录为项目根
os.chdir(Path(__file__).resolve().parent)

# 将 stdout/stderr 重定向到日志文件
log_dir = Path("data") / "logs"
log_dir.mkdir(parents=True, exist_ok=True)
log_file = log_dir / "launcher.log"

sys.stdout = open(log_file, "a", encoding="utf-8")
sys.stderr = sys.stdout

from sirius_pulse.cli import main

raise SystemExit(main())
