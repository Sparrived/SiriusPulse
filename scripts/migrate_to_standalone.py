#!/usr/bin/env python
"""从多人格架构迁移到独立人格架构。

将 data/personas/<name>/ 下的人格数据移动到 data/ 根目录，
使每个人格实例成为独立安装。

用法::

    # 自动选择（仅一个人格时）
    python scripts/migrate_to_standalone.py

    # 指定人格
    python scripts/migrate_to_standalone.py --persona sirius

    # 预览模式（不实际移动）
    python scripts/migrate_to_standalone.py --persona sirius --dry-run
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path


def find_personas(data_dir: Path) -> list[str]:
    """列出 data/personas/ 下的所有人格目录名。"""
    personas_dir = data_dir / "personas"
    if not personas_dir.exists():
        return []
    return [d.name for d in sorted(personas_dir.iterdir()) if d.is_dir()]


def validate_source(personas_dir: Path, name: str) -> Path:
    """验证源人格目录存在且包含必要文件。"""
    source = personas_dir / name
    if not source.is_dir():
        print(f"✗ 人格目录不存在: {source}")
        sys.exit(1)

    required = ["persona.json", "adapters.json"]
    missing = [f for f in required if not (source / f).exists()]
    if missing:
        print(f"✗ 人格目录缺少必要文件: {', '.join(missing)}")
        sys.exit(1)

    return source


def check_target_conflicts(data_dir: Path, source: Path) -> list[str]:
    """检查 data/ 目录中是否已有同名文件/目录（排除 personas/ 和 adapter_port_registry.json）。"""
    conflicts = []
    for item in source.iterdir():
        target = data_dir / item.name
        if target.exists():
            conflicts.append(item.name)
    return conflicts


def migrate(data_dir: Path, persona_name: str, *, dry_run: bool = False) -> None:
    """执行迁移。"""
    personas_dir = data_dir / "personas"
    source = validate_source(personas_dir, persona_name)

    print(f"迁移人格「{persona_name}」到独立安装模式")
    print(f"  源目录: {source}")
    print(f"  目标:   {data_dir}")
    print()

    # 检查冲突
    conflicts = check_target_conflicts(data_dir, source)
    if conflicts:
        print("⚠ 以下文件/目录在 data/ 中已存在:")
        for name in conflicts:
            print(f"    - {name}")
        print()
        if not dry_run:
            answer = input("覆盖这些文件？[y/N] ").strip().lower()
            if answer not in ("y", "yes", "是"):
                print("已取消迁移。")
                sys.exit(0)

    # 列出要移动的项目
    items_to_move = []
    for item in sorted(source.iterdir()):
        items_to_move.append(item.name)

    print("将移动以下内容:")
    for name in items_to_move:
        src = source / name
        dst = data_dir / name
        kind = "目录" if src.is_dir() else "文件"
        action = "覆盖" if dst.exists() else "新增"
        print(f"  [{action}] {kind}: {name}")

    # 移动 providers/ 的合并逻辑
    src_providers = source / "providers"
    dst_providers = data_dir / "providers"
    if src_providers.is_dir() and dst_providers.is_dir():
        print()
        print("  [合并] providers/ 目录（保留现有文件，添加新的）")

    print()

    if dry_run:
        print("(预览模式，未实际执行)")
        return

    # 执行迁移
    for name in items_to_move:
        src = source / name
        dst = data_dir / name

        # providers/ 特殊处理：合并而非覆盖
        if name == "providers" and src.is_dir() and dst.is_dir():
            for provider_file in src.iterdir():
                provider_dst = dst / provider_file.name
                if not provider_dst.exists():
                    shutil.move(str(provider_file), str(provider_dst))
                    print(f"  合并: providers/{provider_file.name}")
            continue

        # 常规移动
        if dst.exists():
            if dst.is_dir():
                shutil.rmtree(dst)
            else:
                dst.unlink()
        shutil.move(str(src), str(dst))
        print(f"  移动: {name}")

    # 移动 global_config.json（如果人格目录里有的话，合并到现有的）
    src_global = source / "global_config.json"
    dst_global = data_dir / "global_config.json"
    if src_global.exists() and dst_global.exists():
        try:
            src_data = json.loads(src_global.read_text(encoding="utf-8"))
            dst_data = json.loads(dst_global.read_text(encoding="utf-8"))
            dst_data.update(src_data)
            dst_global.write_text(
                json.dumps(dst_data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            print("  合并: global_config.json")
        except Exception:
            pass

    # 清理旧的多人格结构
    print()
    print("清理多人格结构...")

    # 删除已空的人格目录
    remaining = list(source.iterdir())
    if not remaining:
        source.rmdir()
        print(f"  删除: personas/{persona_name}/")
    else:
        print(f"  保留: personas/{persona_name}/ (仍有 {len(remaining)} 个未迁移项)")

    # 删除空的 personas/ 目录
    if personas_dir.exists() and not any(personas_dir.iterdir()):
        personas_dir.rmdir()
        print("  删除: personas/")

    # 删除端口注册表
    port_registry = data_dir / "adapter_port_registry.json"
    if port_registry.exists():
        port_registry.unlink()
        print("  删除: adapter_port_registry.json")

    print()
    print("✓ 迁移完成！")
    print()
    print("下一步:")
    print("  1. 检查 data/adapters.json 中的 ws_url 是否正确")
    print("  2. 检查 data/providers/provider_keys.json 是否完整")
    print("  3. 运行: python main.py run")


def main() -> None:
    parser = argparse.ArgumentParser(description="从多人格架构迁移到独立人格架构")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data"),
        help="数据目录路径（默认: data/）",
    )
    parser.add_argument(
        "--persona",
        type=str,
        default=None,
        help="要迁移的人格名称（仅一个人格时自动选择）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="预览模式，不实际移动文件",
    )
    args = parser.parse_args()

    data_dir = args.data_dir.resolve()
    if not data_dir.is_dir():
        print(f"✗ 数据目录不存在: {data_dir}")
        sys.exit(1)

    personas = find_personas(data_dir)
    if not personas:
        print("✗ 未找到 data/personas/ 下的人格目录")
        print("  如果你的项目已经是独立安装模式，无需迁移。")
        sys.exit(1)

    # 选择人格
    persona_name = args.persona
    if persona_name is None:
        if len(personas) == 1:
            persona_name = personas[0]
            print(f"自动选择唯一人格: {persona_name}")
        else:
            print("找到多个人格，请指定要迁移的一个:")
            for i, name in enumerate(personas, 1):
                print(f"  {i}. {name}")
            print()
            choice = input("输入人格名称或序号: ").strip()
            if choice.isdigit():
                index = int(choice) - 1
                if 0 <= index < len(personas):
                    persona_name = personas[index]
            elif choice in personas:
                persona_name = choice
            if persona_name is None:
                print("无效选择。")
                sys.exit(1)
    elif persona_name not in personas:
        print(f"✗ 人格「{persona_name}」不存在于 data/personas/ 中")
        print(f"  可用人格: {', '.join(personas)}")
        sys.exit(1)

    migrate(data_dir, persona_name, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
