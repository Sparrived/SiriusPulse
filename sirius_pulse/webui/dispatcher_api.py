"""Read-only WebUI data for the cross-persona group dispatcher."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from aiohttp import web

from sirius_pulse.core.group_dispatcher import GroupDispatcher
from sirius_pulse.webui.server_utils import _json_response

_DEFAULT_DB_NAME = "dispatcher/dispatcher.db"
_POLICY_KEYS = (
    "dispatch_min_reply_interval_seconds",
    "dispatch_lease_seconds",
    "dispatch_peer_cooldown_seconds",
    "dispatch_max_peer_turns",
    "dispatch_score_collection_seconds",
    "dispatch_activity_window_seconds",
    "dispatch_activity_penalty_per_reply",
    "dispatch_max_activity_penalty",
)


def _persona_dirs(data_dir: Path) -> list[Path]:
    personas_dir = data_dir / "personas"
    if not personas_dir.exists():
        return [data_dir] if (data_dir / "adapters.json").exists() else []
    return [path for path in sorted(personas_dir.iterdir()) if path.is_dir()]


def _read_adapter_configs(data_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    configs: list[tuple[Path, dict[str, Any]]] = []
    for persona_dir in _persona_dirs(data_dir):
        path = persona_dir / "adapters.json"
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for adapter in payload.get("adapters", []) if isinstance(payload, dict) else []:
            if isinstance(adapter, dict):
                configs.append((persona_dir, adapter))
    return configs


def _candidate_db_paths(data_dir: Path, configs: list[tuple[Path, dict[str, Any]]]) -> list[Path]:
    paths: list[Path] = [data_dir / _DEFAULT_DB_NAME]
    for persona_dir, config in configs:
        raw = str(config.get("dispatch_db_path") or "").strip()
        if not raw:
            paths.append(data_dir / _DEFAULT_DB_NAME)
            continue
        configured = Path(raw)
        if configured.is_absolute():
            paths.append(configured)
            continue
        # Runtime workers historically interpret relative paths from process
        # cwd; include the persona-local interpretation for portable installs.
        paths.extend((Path.cwd() / configured, persona_dir / configured, data_dir / configured))
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path.resolve())
        if key not in seen:
            seen.add(key)
            unique.append(path.resolve())
    return unique


def _merge_snapshots(snapshots: list[dict[str, object]], paths: list[Path]) -> dict[str, object]:
    if not snapshots:
        return {
            "available": False,
            "db_path": str(paths[0]) if paths else "",
            "databases": [{"path": str(path), "available": False} for path in paths],
        }

    workers: dict[str, dict[str, object]] = {}
    groups: dict[str, dict[str, object]] = {}
    events: dict[str, dict[str, object]] = {}
    for snapshot in snapshots:
        for item in snapshot.get("workers", []):
            if isinstance(item, dict):
                worker_id = str(item.get("worker_id") or "")
                previous = workers.get(worker_id, {})
                workers[worker_id] = {
                    **previous,
                    **item,
                    "online": bool(previous.get("online") or item.get("online")),
                    "last_seen": max(
                        float(previous.get("last_seen") or 0), float(item.get("last_seen") or 0)
                    ),
                    "last_reply_at": max(
                        float(previous.get("last_reply_at") or 0),
                        float(item.get("last_reply_at") or 0),
                    ),
                    "reply_count": max(
                        int(previous.get("reply_count") or 0), int(item.get("reply_count") or 0)
                    ),
                    "recent_reply_count": max(
                        int(previous.get("recent_reply_count") or 0),
                        int(item.get("recent_reply_count") or 0),
                    ),
                }
        for item in snapshot.get("groups", []):
            if isinstance(item, dict):
                group_id = str(item.get("group_id") or "")
                previous = groups.get(group_id)
                if (
                    previous is None
                    or bool(item.get("active"))
                    or float(item.get("last_reply_at") or 0)
                    > float(previous.get("last_reply_at") or 0)
                ):
                    groups[group_id] = item
        for item in snapshot.get("events", []):
            if isinstance(item, dict):
                event_id = str(item.get("event_id") or "")
                previous = events.get(event_id)
                if previous is None or float(item.get("updated_at") or 0) > float(
                    previous.get("updated_at") or 0
                ):
                    events[event_id] = item

    merged_events = sorted(
        events.values(), key=lambda item: float(item.get("updated_at") or 0), reverse=True
    )[:200]
    merged_workers = sorted(
        workers.values(),
        key=lambda item: (
            -int(bool(item.get("online"))),
            -float(item.get("priority") or 0),
            str(item.get("worker_id") or ""),
        ),
    )
    merged_groups = sorted(
        groups.values(),
        key=lambda item: (
            -int(bool(item.get("active"))),
            -float(item.get("last_reply_at") or 0),
            str(item.get("group_id") or ""),
        ),
    )[:200]
    now = max(float(snapshot.get("updated_at") or 0) for snapshot in snapshots)
    day_ago = now - 86400.0
    recent = [item for item in merged_events if float(item.get("updated_at") or 0) >= day_ago]
    return {
        "available": True,
        "db_path": str(snapshots[0].get("db_path") or paths[0]),
        "updated_at": now,
        "databases": [
            {"path": str(snapshot.get("db_path") or ""), "available": True}
            for snapshot in snapshots
        ],
        "workers": merged_workers,
        "groups": merged_groups,
        "events": merged_events,
        "summary": {
            "workers_total": len(merged_workers),
            "workers_online": sum(1 for item in merged_workers if item.get("online")),
            "groups_total": len(merged_groups),
            "active_turns": sum(1 for item in merged_groups if item.get("active")),
            "decisions_24h": len(recent),
            "granted_24h": sum(
                1
                for item in recent
                if item.get("status") in {"granted", "sent", "silent", "expired"}
            ),
            "sent_24h": sum(1 for item in recent if item.get("status") == "sent"),
            "observed_24h": sum(1 for item in recent if item.get("status") == "observed"),
        },
    }


def _policy(configs: list[tuple[Path, dict[str, Any]]]) -> dict[str, object]:
    enabled = [
        config
        for _, config in configs
        if bool(config.get("enabled", True)) and bool(config.get("group_dispatch_enabled", True))
    ]
    values: dict[str, list[float | int]] = {key: [] for key in _POLICY_KEYS}
    for config in enabled:
        for key in _POLICY_KEYS:
            if key in config:
                try:
                    values[key].append(float(config[key]))
                except (TypeError, ValueError):
                    continue
    return {
        "adapters_total": len(configs),
        "adapters_enabled": len(enabled),
        "dispatch_enabled": bool(enabled),
        "values": {
            key: (sorted(set(items))[0] if len(set(items)) == 1 else sorted(set(items)))
            for key, items in values.items()
            if items
        },
    }


async def api_dispatcher_overview(request: web.Request, data_dir: Path) -> web.Response:
    """Return live dispatcher state for the operations console."""
    configs = _read_adapter_configs(data_dir)
    paths = _candidate_db_paths(data_dir, configs)
    snapshots = []
    for path in paths:
        # 与人格共用事件循环：读库放进线程，别让 WebUI 轮询卡住回复。
        snapshot = await asyncio.to_thread(GroupDispatcher.read_snapshot, path)
        if snapshot.get("available"):
            snapshots.append(snapshot)
    payload = _merge_snapshots(snapshots, paths)
    payload["policy"] = _policy(configs)
    payload["configured_paths"] = [str(path) for path in paths]
    return _json_response(payload)


__all__ = ["api_dispatcher_overview"]
