#!/usr/bin/env python3
"""Compact oversized Sirius Pulse history while preserving a rollback copy.

Run only while the persona worker is stopped. The script streams archive JSONL
files, removes duplicate prompt/request snapshots, and rewrites the active
basic-memory snapshot to a bounded recent window.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from uuid import uuid4

MAX_CHAIN_MESSAGES = 12
MAX_CHAIN_CONTENT_CHARS = 4_000
MAX_SYSTEM_PROMPT_CHARS = 8_000
DEFAULT_WINDOW = 30
MAX_SNAPSHOT_LOAD_BYTES = 64 * 1024 * 1024


def _truncate(value: object, limit: int) -> object:
    if not isinstance(value, str) or len(value) <= limit:
        return value
    return f"{value[:limit]}\n…[迁移时截断 {len(value) - limit} 个字符]"


def _compact_entry(entry: dict[str, object]) -> dict[str, object]:
    result = dict(entry)
    chain = result.get("conversation_chain")
    if isinstance(chain, list):
        messages = [message for message in chain if isinstance(message, dict)]
        if len(messages) > MAX_CHAIN_MESSAGES:
            first = messages[0]
            tail_size = (
                MAX_CHAIN_MESSAGES - 1 if first.get("role") == "system" else MAX_CHAIN_MESSAGES
            )
            messages = ([first] if first.get("role") == "system" else []) + messages[-tail_size:]
        compacted: list[dict[str, object]] = []
        for message in messages:
            item = dict(message)
            item["content"] = _truncate(item.get("content"), MAX_CHAIN_CONTENT_CHARS)
            compacted.append(item)
        if compacted and compacted[0].get("role") == "system":
            compacted[0]["content"] = _truncate(
                compacted[0].get("content"), MAX_SYSTEM_PROMPT_CHARS
            )
        result["conversation_chain"] = compacted
    result.pop("injected_request", None)
    return result


def _fsync_replace(tmp: Path, target: Path) -> None:
    with tmp.open("a", encoding="utf-8") as f:
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)


def _compact_archive(path: Path, backup_dir: Path) -> tuple[int, int, int]:
    backup = backup_dir / "archive" / path.name
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup)

    tmp = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    total = written = invalid = 0
    try:
        with path.open("r", encoding="utf-8") as src, tmp.open("w", encoding="utf-8") as dst:
            for raw_line in src:
                total += 1
                try:
                    entry = json.loads(raw_line)
                except json.JSONDecodeError:
                    invalid += 1
                    dst.write(raw_line)
                    continue
                if not isinstance(entry, dict):
                    invalid += 1
                    dst.write(raw_line)
                    continue
                dst.write(json.dumps(_compact_entry(entry), ensure_ascii=False) + "\n")
                written += 1
        _fsync_replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return total, written, invalid


def _compact_snapshot(path: Path, backup_dir: Path, window: int) -> dict[str, int]:
    if path.stat().st_size > MAX_SNAPSHOT_LOAD_BYTES:
        raise ValueError(
            f"{path} is too large for safe in-process compaction; archive it and let the service recreate it"
        )
    backup = backup_dir / "engine_state" / path.name
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup)
    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise ValueError("basic_memory.json must contain a JSON object")
    compacted = {
        str(group_id): [
            _compact_entry(entry) for entry in entries[-window:] if isinstance(entry, dict)
        ]
        for group_id, entries in raw.items()
        if isinstance(entries, list)
    }
    tmp = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(compacted, f, ensure_ascii=False, indent=2)
        _fsync_replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return {group_id: len(entries) for group_id, entries in compacted.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("persona_dir", type=Path, help="data/personas/<name> directory")
    parser.add_argument(
        "--window", type=int, default=DEFAULT_WINDOW, help="active entries retained per group"
    )
    parser.add_argument(
        "--apply", action="store_true", help="perform compaction; default is a dry run"
    )
    args = parser.parse_args()
    if args.window < 1:
        parser.error("--window must be positive")

    persona_dir = args.persona_dir.resolve()
    archive_dir = persona_dir / "archive"
    state_path = persona_dir / "engine_state" / "basic_memory.json"
    archives = sorted(archive_dir.glob("*.jsonl")) if archive_dir.exists() else []
    print(f"archives={len(archives)} snapshot_exists={state_path.exists()} apply={args.apply}")
    if not args.apply:
        for path in archives:
            print(f"would compact {path} ({path.stat().st_size} bytes)")
        if state_path.exists():
            print(f"would compact {state_path} ({state_path.stat().st_size} bytes)")
        return 0

    backup_dir = persona_dir / "maintenance_backups" / datetime.now().strftime("%Y%m%dT%H%M%S")
    for path in archives:
        total, written, invalid = _compact_archive(path, backup_dir)
        print(f"compacted {path.name}: lines={total} json={written} invalid={invalid}")
    if state_path.exists():
        if state_path.stat().st_size > MAX_SNAPSHOT_LOAD_BYTES:
            backup = backup_dir / "engine_state" / state_path.name
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(state_path, backup)
            print("archived oversized basic_memory.json; service will recreate a bounded snapshot")
        else:
            retained = _compact_snapshot(state_path, backup_dir, args.window)
            print(f"compacted basic_memory.json: retained={retained}")
    print(f"backup={backup_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
