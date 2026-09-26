"""WebUI API endpoints for memory, tokens and cognition data."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from heapq import heappush, heapreplace
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from aiohttp import web

from sirius_pulse.memory.basic.file_lock import archive_file_lock
from sirius_pulse.memory.units.deduplicator import normalize_summary
from sirius_pulse.persona_config import PersonaConfigPaths
from sirius_pulse.utils.json_io import replace_with_retry
from sirius_pulse.webui.persona_manager_api import _is_persona_running
from sirius_pulse.webui.server_utils import _json_response, handle_api_errors

LOG = logging.getLogger("sirius.webui")
_ACTIVE_DEDUPE_STATES = {"queued", "scanning", "ready", "applying"}
_MAX_RUNTIME_BASIC_MEMORY_BYTES = 64 * 1024 * 1024


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_memory_name(name: str) -> str:
    import re

    base = re.sub(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]+", "_", name.strip())
    base = re.sub(r"_+", "_", base).strip("_")
    return base or "default"


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    replace_with_retry(tmp, path)


def _memory_dedupe_dir(data_dir: Path) -> Path:
    return data_dir / "engine_state" / "memory_dedupe"


def _read_json_dict(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _dedupe_status(data_dir: Path) -> dict[str, Any]:
    return _read_json_dict(_memory_dedupe_dir(data_dir) / "status.json")


def _memory_units_applying(data_dir: Path) -> bool:
    return _dedupe_status(data_dir).get("status") == "applying"


def _queue_memory_reconcile(data_dir: Path, *, group_ids: list[str], unit_ids: list[str]) -> None:
    path = _memory_dedupe_dir(data_dir) / "reconcile.json"
    current = _read_json_dict(path)
    _atomic_write_json(
        path,
        {
            "group_ids": sorted(set(current.get("group_ids") or []) | set(group_ids)),
            "unit_ids": sorted(set(current.get("unit_ids") or []) | set(unit_ids)),
        },
    )


def _iter_tail_lines(path: Path, n: int) -> Iterator[str]:
    """Yield at most n non-empty JSONL records from newest to oldest."""
    with path.open("rb") as f:
        f.seek(0, 2)
        pos = f.tell()
        buffer = b""
        yielded = 0
        while pos > 0 and yielded < n:
            read_size = min(8192, pos)
            pos -= read_size
            f.seek(pos)
            buffer = f.read(read_size) + buffer
            parts = buffer.split(b"\n")
            buffer = parts[0]
            for part in reversed(parts[1:]):
                stripped = part.strip()
                if stripped:
                    yielded += 1
                    yield stripped.decode("utf-8", errors="replace")
                    if yielded >= n:
                        return
        stripped = buffer.strip()
        if stripped and yielded < n:
            yield stripped.decode("utf-8", errors="replace")


def _read_tail_lines(path: Path, n: int) -> list[str]:
    """Backward-compatible list wrapper for small callers."""
    return list(_iter_tail_lines(path, n))


def _conversation_entry_key(entry: dict[str, Any]) -> str:
    entry_id = str(entry.get("entry_id") or "").strip()
    if entry_id:
        return f"id:{entry_id}"
    timestamp = str(entry.get("timestamp") or "")
    role = str(entry.get("role") or "")
    user_id = str(entry.get("user_id") or "")
    content = str(entry.get("content") or "")[:120]
    return f"fallback:{timestamp}:{role}:{user_id}:{content}"


def _truncate_history_text(value: Any, limit: int) -> Any:
    if not isinstance(value, str) or len(value) <= limit:
        return value
    return f"{value[:limit]}\n…[历史记录已截断 {len(value) - limit} 个字符]"


def _compact_history_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Bound one legacy archive entry before returning it to the WebUI."""
    result = dict(entry)
    result["content"] = _truncate_history_text(result.get("content"), 8_000)
    result["system_prompt"] = _truncate_history_text(result.get("system_prompt"), 8_000)
    result["reasoning_content"] = _truncate_history_text(result.get("reasoning_content"), 4_000)

    chain = result.get("conversation_chain")
    if isinstance(chain, list):
        messages = [message for message in chain if isinstance(message, dict)]
        if len(messages) > 12:
            first = messages[0]
            messages = ([first] if first.get("role") == "system" else []) + messages[-11:]
        result["conversation_chain"] = [
            {
                **message,
                "content": _truncate_history_text(message.get("content"), 4_000),
                "reasoning_content": _truncate_history_text(
                    message.get("reasoning_content"), 2_000
                ),
            }
            for message in messages
        ]

    # Legacy records duplicate the complete prompt and tool schemas here. The
    # bounded conversation_chain above is sufficient for history inspection.
    result["injected_request"] = {}
    return result


def _load_runtime_basic_memory_messages(paths: Any, group_id: str = "") -> list[dict[str, Any]]:
    """Load the active basic-memory window used for prompt assembly.

    The archive files are append-only display history. The active prompt context
    is restored from engine_state/basic_memory.json, so exposing it here lets the
    WebUI inspect the same recent LLM chains that generation uses.
    """
    state_path = paths.engine_state / "basic_memory.json"
    if not state_path.exists():
        return []
    try:
        if state_path.stat().st_size > _MAX_RUNTIME_BASIC_MEMORY_BYTES:
            LOG.warning("跳过过大的运行态基础记忆快照: %s", state_path)
            return []
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return []

    if not isinstance(raw, dict):
        return []

    messages: list[dict[str, Any]] = []
    for gid, entries in raw.items():
        gid_text = str(gid)
        if group_id and gid_text != group_id:
            continue
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            item = dict(entry)
            item["group_id"] = item.get("group_id") or gid_text
            if not item.get("tags"):
                item["tags"] = []
            messages.append(item)
    return messages


def _merge_conversation_messages(
    archive_messages: list[dict[str, Any]],
    runtime_messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    for source in (archive_messages, runtime_messages):
        for message in source:
            key = _conversation_entry_key(message)
            if key not in merged:
                order.append(key)
                merged[key] = message
            else:
                merged[key] = {**merged[key], **message}

    return [merged[key] for key in order]


def _load_compressed_memory_source_index(
    paths: PersonaConfigPaths,
    group_id: str = "",
) -> dict[str, list[dict[str, Any]]]:
    """Return basic-memory source_id -> compact memory references."""
    refs_by_source: dict[str, list[dict[str, Any]]] = {}

    def add_ref(source_ids: Any, ref: dict[str, Any]) -> None:
        if not isinstance(source_ids, list):
            return
        for source_id in source_ids:
            key = str(source_id or "").strip()
            if key:
                refs_by_source.setdefault(key, []).append(ref)

    units_dir = paths.dir / "memory_units"
    if units_dir.exists():
        unit_files = (
            [units_dir / f"{_safe_memory_name(group_id)}.json"]
            if group_id
            else sorted(units_dir.glob("*.json"))
        )
        for path in unit_files:
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, TypeError):
                continue
            units = data.get("units", []) if isinstance(data, dict) else []
            if not isinstance(units, list):
                continue
            for unit in units:
                if not isinstance(unit, dict):
                    continue
                add_ref(
                    unit.get("source_ids"),
                    {
                        "kind": "memory_unit",
                        "id": str(unit.get("unit_id") or ""),
                        "summary": str(unit.get("summary") or "")[:180],
                        "created_at": str(unit.get("created_at") or ""),
                        "unit_type": str(unit.get("unit_type") or ""),
                    },
                )

    return refs_by_source


def _annotate_memory_compression(
    messages: list[dict[str, Any]],
    source_index: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    if not source_index:
        for message in messages:
            message["memory_compressed"] = False
            message["memory_refs"] = []
        return messages

    for message in messages:
        entry_id = str(message.get("entry_id") or "").strip()
        refs = list(source_index.get(entry_id, [])) if entry_id else []
        message["memory_compressed"] = bool(refs)
        message["memory_refs"] = refs
    return messages


def _conversation_message_matches_filters(
    message: dict[str, Any],
    *,
    search: str,
    speaker: str,
    start_time: str,
    end_time: str,
) -> bool:
    if search and search not in (message.get("content", "") or "").lower():
        return False
    if speaker:
        speaker_name = (message.get("speaker_name", "") or "").lower()
        user_id = (message.get("user_id", "") or "").lower()
        if speaker not in speaker_name and speaker not in user_id:
            return False
    if start_time and message.get("timestamp", "") < start_time:
        return False
    if end_time and message.get("timestamp", "") > end_time:
        return False
    return True


def _conversation_key_from_query(request: web.Request) -> str:
    key = request.query.get("key", "").strip()
    if key:
        return key

    entry_id = request.query.get("entry_id", "").strip()
    if entry_id:
        return f"id:{entry_id}"

    timestamp = request.query.get("timestamp", "")
    role = request.query.get("role", "")
    user_id = request.query.get("user_id", "")
    content = request.query.get("content", "")[:120]
    if timestamp or role or user_id or content:
        return f"fallback:{timestamp}:{role}:{user_id}:{content}"
    return ""


def _rewrite_jsonl_without_conversation_key(path: Path, group_id: str, key: str) -> int:
    """Remove a message via a streamed, crash-safe JSONL rewrite."""
    if not path.exists():
        return 0

    with archive_file_lock(path):
        if not path.exists():
            return 0
        deleted = 0
        tmp = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
        try:
            with path.open("r", encoding="utf-8") as src, tmp.open("w", encoding="utf-8") as dst:
                for raw_line in src:
                    stripped = raw_line.strip()
                    if not stripped:
                        dst.write(raw_line)
                        continue
                    try:
                        entry = json.loads(stripped)
                    except json.JSONDecodeError:
                        dst.write(raw_line)
                        continue
                    if not isinstance(entry, dict):
                        dst.write(raw_line)
                        continue
                    entry_with_group = dict(entry)
                    entry_with_group["group_id"] = entry_with_group.get("group_id") or group_id
                    if _conversation_entry_key(entry_with_group) == key:
                        deleted += 1
                        continue
                    dst.write(raw_line)
                dst.flush()
                os.fsync(dst.fileno())
            if deleted:
                replace_with_retry(tmp, path)
            return deleted
        except OSError:
            return 0
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def _delete_runtime_basic_memory_message(paths: Any, key: str, group_id: str = "") -> int:
    state_path = paths.engine_state / "basic_memory.json"
    if not state_path.exists():
        return 0

    try:
        if state_path.stat().st_size > _MAX_RUNTIME_BASIC_MEMORY_BYTES:
            LOG.warning("拒绝重写过大的运行态基础记忆快照: %s", state_path)
            return 0
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return 0

    if not isinstance(raw, dict):
        return 0

    deleted = 0
    changed = False
    for gid, entries in list(raw.items()):
        gid_text = str(gid)
        if group_id and gid_text != group_id:
            continue
        if not isinstance(entries, list):
            continue
        kept: list[Any] = []
        for entry in entries:
            if not isinstance(entry, dict):
                kept.append(entry)
                continue
            item = dict(entry)
            item["group_id"] = item.get("group_id") or gid_text
            if _conversation_entry_key(item) == key:
                deleted += 1
                changed = True
                continue
            kept.append(entry)
        raw[gid] = kept

    if changed:
        _atomic_write_json(state_path, raw)
    return deleted


async def api_tokens_get(request: web.Request, data_dir: Path) -> web.Response:
    """Return token usage for the current persona."""
    from sirius_pulse.token import analytics as token_analytics
    from sirius_pulse.token.token_store import TokenUsageStore

    db_path = data_dir / "persona.db"
    total_summary = {
        "total_calls": 0,
        "total_prompt_tokens": 0,
        "total_completion_tokens": 0,
        "total_tokens": 0,
    }
    cache_stats: dict[str, Any] = {
        "total_calls": 0,
        "cache_info_calls": 0,
        "cache_info_coverage_pct": 0.0,
        "cached_prompt_tokens": 0,
        "uncached_prompt_tokens": 0,
        "cache_creation_prompt_tokens": 0,
        "cache_hit_rate_pct": 0.0,
    }

    if db_path.exists():
        try:
            store = TokenUsageStore(str(db_path), read_only=True)
            baseline = token_analytics.compute_baseline(store)
            total_summary["total_calls"] = baseline.get("total_calls", 0)
            total_summary["total_prompt_tokens"] = baseline.get("total_prompt_tokens", 0)
            total_summary["total_completion_tokens"] = baseline.get("total_completion_tokens", 0)
            total_summary["total_tokens"] = baseline.get("total_tokens", 0)
            cache_stats = store.get_cache_stats()
        except Exception as exc:
            LOG.warning("读取 Token 统计失败: %s", exc)

    response_avg: dict[str, Any] = {
        "total_calls": 0,
        "avg_total_tokens": 0,
        "avg_prompt_tokens": 0,
        "avg_completion_tokens": 0,
    }
    if total_summary["total_calls"]:
        response_avg = {
            "total_calls": total_summary["total_calls"],
            "avg_total_tokens": round(
                total_summary["total_tokens"] / total_summary["total_calls"], 1
            ),
            "avg_prompt_tokens": round(
                total_summary["total_prompt_tokens"] / total_summary["total_calls"], 1
            ),
            "avg_completion_tokens": round(
                total_summary["total_completion_tokens"] / total_summary["total_calls"], 1
            ),
        }

    return _json_response(
        {
            "summary": total_summary,
            "response_avg": response_avg,
            "cache_stats": cache_stats,
        }
    )


async def api_telemetry_get(request: web.Request, data_dir: Path) -> web.Response:
    """Return tool usage telemetry for the current persona."""
    all_summaries: dict[str, dict[str, Any]] = {}
    total_calls = 0

    telemetry_path = data_dir / "tool_data" / ".telemetry.jsonl"
    if telemetry_path.exists():
        try:
            with open(telemetry_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    record = json.loads(line)
                    tool_name = record.get("tool_name", "unknown")
                    if tool_name not in all_summaries:
                        all_summaries[tool_name] = {
                            "calls": 0,
                            "successes": 0,
                            "failures": 0,
                            "total_ms": 0.0,
                        }
                    agg = all_summaries[tool_name]
                    agg["calls"] += 1
                    total_calls += 1
                    if record.get("success"):
                        agg["successes"] += 1
                    else:
                        agg["failures"] += 1
                    agg["total_ms"] += record.get("duration_ms", 0)
        except Exception as exc:
            LOG.warning("读取 Telemetry 失败: %s", exc)

    tools: dict[str, Any] = {}
    for tool_name, stats in all_summaries.items():
        calls = stats["calls"]
        tools[tool_name] = {
            "calls": calls,
            "success_rate": round(stats["successes"] / calls * 100, 1) if calls else 0,
            "avg_ms": round(stats["total_ms"] / calls, 1) if calls else 0,
        }

    return _json_response(
        {
            "total_calls": total_calls,
            "tools": tools,
        }
    )


@handle_api_errors
async def api_persona_tokens_get(request: web.Request, data_dir: Path) -> web.Response:
    paths = PersonaConfigPaths(data_dir)

    from sirius_pulse.token import analytics as token_analytics
    from sirius_pulse.token.token_store import TokenUsageStore

    db_path = paths.dir / "persona.db"
    if not db_path.exists():
        return _json_response(
            {
                "summary": {
                    "total_calls": 0,
                    "total_prompt_tokens": 0,
                    "total_completion_tokens": 0,
                    "total_tokens": 0,
                },
                "cache_stats": {
                    "total_calls": 0,
                    "cache_info_calls": 0,
                    "cache_info_coverage_pct": 0.0,
                    "cached_prompt_tokens": 0,
                    "uncached_prompt_tokens": 0,
                    "cache_creation_prompt_tokens": 0,
                    "cache_hit_rate_pct": 0.0,
                },
                "hourly": [],
                "by_model": [],
                "by_group": [],
                "by_provider": [],
                "by_task": [],
                "recent_with_breakdown": [],
            }
        )

    # Parse optional time range from query params
    start_ts: float | None = None
    end_ts: float | None = None
    try:
        if request.query.get("start"):
            start_ts = float(request.query["start"])
        if request.query.get("end"):
            end_ts = float(request.query["end"])
    except ValueError:
        LOG.warning("解析时间范围查询参数失败", exc_info=True)
        pass

    store = TokenUsageStore(str(db_path), read_only=True)
    baseline = token_analytics.compute_baseline(store, start_ts=start_ts, end_ts=end_ts)
    cache_stats = store.get_cache_stats(start_ts=start_ts, end_ts=end_ts)
    by_model = token_analytics.group_by_model(store, start_ts=start_ts, end_ts=end_ts)
    time_series = token_analytics.time_series(
        store, bucket_seconds=3600, start_ts=start_ts, end_ts=end_ts
    )

    # 转换为前端期望的格式
    summary = {
        "total_calls": baseline.get("total_calls", 0),
        "total_prompt_tokens": baseline.get("total_prompt_tokens", 0),
        "total_completion_tokens": baseline.get("total_completion_tokens", 0),
        "total_tokens": baseline.get("total_tokens", 0),
    }
    response_avg = {}
    if summary["total_calls"]:
        response_avg = {
            "total_calls": summary["total_calls"],
            "avg_total_tokens": round(summary["total_tokens"] / summary["total_calls"], 1),
            "avg_prompt_tokens": round(summary["total_prompt_tokens"] / summary["total_calls"], 1),
            "avg_completion_tokens": round(
                summary["total_completion_tokens"] / summary["total_calls"], 1
            ),
        }

    # hourly 数据（按小时聚合，用于时间序列图）
    hourly = []
    for ts in time_series:
        try:
            dt = datetime.fromisoformat(ts["time_bucket"])
            hour_ts = int(dt.timestamp())
        except Exception:
            LOG.warning("读取 token 文件失败", exc_info=True)
            continue
        hourly.append(
            {
                "hour_ts": hour_ts,
                "hour": dt.hour,
                "calls": ts.get("calls", 0),
                "prompt_tokens": ts.get("prompt_tokens", 0),
                "completion_tokens": ts.get("completion_tokens", 0),
                "total_tokens": ts.get("total_tokens", 0),
                "cached_prompt_tokens": ts.get("cached_prompt_tokens", 0),
                "uncached_prompt_tokens": ts.get("uncached_prompt_tokens", 0),
            }
        )

    # hourly_distribution: 按小时聚合的调用分布
    hourly_distribution: dict[int, int] = {}
    for h in hourly:
        hour = h["hour"]
        hourly_distribution[hour] = hourly_distribution.get(hour, 0) + h["calls"]
    hourly_distribution_list = [
        {"hour": h, "calls": c} for h, c in sorted(hourly_distribution.items())
    ]

    # by_model 转换为前端期望的格式
    by_model_list = [
        {
            "name": m,
            "calls": v.get("calls", 0),
            "prompt_tokens": v.get("prompt_tokens", 0),
            "completion_tokens": v.get("completion_tokens", 0),
            "total_tokens": v.get("total_tokens", 0),
            "cache_info_calls": v.get("cache_info_calls", 0),
            "cached_prompt_tokens": v.get("cached_prompt_tokens", 0),
            "uncached_prompt_tokens": v.get("uncached_prompt_tokens", 0),
            "cache_creation_prompt_tokens": v.get("cache_creation_prompt_tokens", 0),
        }
        for m, v in by_model.items()
    ]

    # 查询各维度 breakdown 数据
    by_group = store.get_breakdown_by("group_id", start_ts=start_ts, end_ts=end_ts)
    by_provider = store.get_breakdown_by("provider_name", start_ts=start_ts, end_ts=end_ts)
    by_task = store.get_breakdown_by("task_name", start_ts=start_ts, end_ts=end_ts)
    section_breakdown = store.get_section_breakdown(start_ts=start_ts, end_ts=end_ts)
    section_breakdown_by_task = store.get_section_breakdown_by_task(
        start_ts=start_ts, end_ts=end_ts
    )
    recent_with_breakdown = store.get_recent_records_with_breakdown(
        limit=100, start_ts=start_ts, end_ts=end_ts
    )

    # 统计指标
    total_tokens = summary["total_tokens"]
    prompt_tokens = summary["total_prompt_tokens"]
    completion_tokens = summary["total_completion_tokens"]
    ratio = {}
    if total_tokens:
        ratio = {
            "prompt_pct": round(prompt_tokens * 100.0 / total_tokens, 1),
            "completion_pct": round(completion_tokens * 100.0 / total_tokens, 1),
        }

    return _json_response(
        {
            "summary": summary,
            "cache_stats": cache_stats,
            "response_avg": response_avg,
            "hourly": hourly,
            "hourly_distribution": hourly_distribution_list,
            "by_model": by_model_list,
            "by_group": by_group,
            "by_provider": by_provider,
            "by_task": by_task,
            "section_breakdown": section_breakdown,
            "section_breakdown_by_task": section_breakdown_by_task,
            "recent_with_breakdown": recent_with_breakdown,
            "ratio": ratio,
            "efficiency_stats": store.get_efficiency_stats(start_ts=start_ts, end_ts=end_ts),
            "retry_stats": store.get_retry_stats(start_ts=start_ts, end_ts=end_ts),
            "duration_stats": store.get_duration_stats(start_ts=start_ts, end_ts=end_ts),
            "empty_reply_stats": store.get_empty_reply_stats(start_ts=start_ts, end_ts=end_ts),
            "failure_stats": store.get_failure_stats(start_ts=start_ts, end_ts=end_ts),
            "depth_stats": store.get_conversation_depth_stats(start_ts=start_ts, end_ts=end_ts),
            "period_comparison": store.get_period_comparison(start_ts=start_ts, end_ts=end_ts),
        }
    )


@handle_api_errors
async def api_persona_cognition_get(request: web.Request, data_dir: Path) -> web.Response:
    paths = PersonaConfigPaths(data_dir)

    db_path = paths.dir / "persona.db"
    if not db_path.exists():
        return _json_response({"events": [], "emotion_distribution": {}})

    from sirius_pulse.memory.cognition_store import CognitionEventStore

    store = CognitionEventStore(str(db_path), read_only=True)
    limit = int(request.query.get("limit", "50"))
    events = store.get_recent(limit=limit)
    group_id = request.query.get("group_id", None)
    emotion_distribution = store.get_emotion_distribution(group_id=group_id if group_id else None)
    store.close()
    return _json_response({"events": events, "emotion_distribution": emotion_distribution})


@handle_api_errors
async def api_persona_cognition_analysis_get(request: web.Request, data_dir: Path) -> web.Response:
    """Return rich cognition analysis: intent/hourly/score distributions + decision stats."""
    paths = PersonaConfigPaths(data_dir)

    db_path = paths.dir / "persona.db"
    if not db_path.exists():
        return _json_response({"has_data": False})

    from sirius_pulse.memory.cognition_store import CognitionEventStore

    store = CognitionEventStore(str(db_path), read_only=True)
    group_id = request.query.get("group_id", None) or None

    result: dict[str, Any] = {"has_data": True}

    # 认知事件聚合
    result["intent_distribution"] = store.get_intent_distribution(group_id=group_id)
    result["group_summary"] = store.get_group_summary()
    result["hourly_distribution"] = store.get_hourly_distribution(group_id=group_id)

    # 分数分布（只取直方图统计，不传原始数组）
    raw_scores = store.get_score_distributions(group_id=group_id)
    result["score_histograms"] = {
        key: _build_histogram(values, bins=10, range_min=0.0, range_max=1.0)
        for key, values in raw_scores.items()
    }

    # 决策事件聚合
    result["strategy_distribution"] = store.get_strategy_distribution(group_id=group_id)
    result["decision_summary"] = store.get_decision_summary(group_id=group_id)
    result["decision_timeline"] = store.get_decision_timeline(group_id=group_id, limit=50)

    store.close()
    return _json_response(result)


def _build_histogram(
    values: list[float], bins: int = 10, range_min: float = 0.0, range_max: float = 1.0
) -> dict[str, Any]:
    """Build a histogram from raw values for frontend rendering."""
    if not values:
        return {"labels": [], "counts": [], "total": 0}
    step = (range_max - range_min) / bins
    labels: list[str] = []
    counts: list[int] = [0] * bins
    for i in range(bins):
        lo = range_min + i * step
        hi = lo + step
        labels.append(f"{lo:.1f}-{hi:.1f}")
    for v in values:
        idx = min(int((v - range_min) / step), bins - 1)
        if 0 <= idx < bins:
            counts[idx] += 1
    return {"labels": labels, "counts": counts, "total": len(values)}


def _memory_units_dir(data_dir: Path) -> Path:
    return data_dir / "memory_units"


def _memory_unit_file(data_dir: Path, group_id: str) -> Path:
    return _memory_units_dir(data_dir) / f"{_safe_memory_name(group_id)}.json"


def _load_memory_units_file(path: Path) -> tuple[str, list[dict[str, Any]]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return path.stem, []
    if not isinstance(data, dict):
        return path.stem, []
    group_id = str(data.get("group_id") or path.stem)
    units = [item for item in data.get("units", []) if isinstance(item, dict)]
    return group_id, units


def _save_memory_units_file(data_dir: Path, group_id: str, units: list[dict[str, Any]]) -> None:
    """写回单元文件，并保留向量 sidecar 的引用与格式版本。

    WebUI 只编辑元数据，向量存放在 ``memory_units/vectors/`` 下的独立文件里。这里
    必须把 ``vector_file`` 原样带回去：丢掉它，整组向量就会变成无人引用的孤儿文件，
    而单元仍带着指向它的偏移——等于一次人工编辑清空该群全部语义检索能力。
    """
    from sirius_pulse.memory.units.store import (
        UNITS_FORMAT_KEY,
        UNITS_FORMAT_VERSION,
        VECTOR_FILE_KEY,
    )

    path = _memory_unit_file(data_dir, group_id)
    payload: dict[str, Any] = {
        "group_id": group_id,
        UNITS_FORMAT_KEY: UNITS_FORMAT_VERSION,
        "units": units,
    }
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        existing = None
    if isinstance(existing, dict) and isinstance(existing.get(VECTOR_FILE_KEY), str):
        payload[VECTOR_FILE_KEY] = existing[VECTOR_FILE_KEY]
    _atomic_write_json(path, payload)


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def _normalize_memory_unit(data: dict[str, Any], group_id: str = "") -> dict[str, Any]:
    normalized = dict(data)
    normalized["unit_id"] = str(normalized.get("unit_id") or f"unit_{uuid4().hex}")
    normalized["group_id"] = str(normalized.get("group_id") or group_id or "default")
    normalized["created_at"] = str(normalized.get("created_at") or _now_iso())
    normalized["unit_type"] = str(normalized.get("unit_type") or "event")
    normalized["scope"] = str(normalized.get("scope") or "group")
    normalized["scope_id"] = str(normalized.get("scope_id") or "")
    normalized["summary"] = str(normalized.get("summary") or "").strip()
    normalized["lifespan"] = str(normalized.get("lifespan") or "medium")
    normalized["participants"] = _string_list(normalized.get("participants"))
    normalized["topics"] = _string_list(normalized.get("topics"))
    normalized["keywords"] = _string_list(normalized.get("keywords"))
    normalized["source_ids"] = _string_list(normalized.get("source_ids"))
    normalized["salience"] = max(0.0, min(1.0, float(normalized.get("salience") or 0)))
    normalized["confidence"] = max(0.0, min(1.0, float(normalized.get("confidence") or 0)))
    normalized["should_prompt"] = bool(normalized.get("should_prompt", True))
    if not isinstance(normalized.get("metadata"), dict):
        normalized["metadata"] = {}
    if normalized.get("embedding") is not None and not isinstance(
        normalized.get("embedding"), list
    ):
        normalized["embedding"] = None
    return normalized


@handle_api_errors
async def api_persona_memory_units_get(request: web.Request, data_dir: Path) -> web.Response:
    """Return checkpoint MemoryUnit records for the current persona."""
    group_filter = request.query.get("group_id", "").strip()
    search = request.query.get("search", "").strip().lower()
    limit = min(int(request.query.get("limit", "200")), 1000)
    offset = max(int(request.query.get("offset", "0")), 0)

    base_dir = _memory_units_dir(data_dir)
    units: list[dict[str, Any]] = []
    groups: set[str] = set()
    if base_dir.exists():
        for path in base_dir.glob("*.json"):
            group_id, file_units = _load_memory_units_file(path)
            groups.add(group_id)
            if group_filter and group_id != group_filter:
                continue
            for unit in file_units:
                normalized = _normalize_memory_unit(unit, group_id)
                if search and search not in json.dumps(normalized, ensure_ascii=False).lower():
                    continue
                units.append(normalized)

    units.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    total = len(units)
    return _json_response(
        {
            "units": units[offset : offset + limit],
            "groups": sorted(groups),
            "total": total,
        }
    )


@handle_api_errors
async def api_persona_memory_units_post(request: web.Request, data_dir: Path) -> web.Response:
    """Create a MemoryUnit record."""
    if _memory_units_applying(data_dir):
        return _json_response({"error": "记忆清理正在应用"}, 409)
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "Invalid JSON"}, 400)
    if not isinstance(body, dict):
        return _json_response({"error": "Invalid JSON"}, 400)

    unit = _normalize_memory_unit(body)
    if not unit["summary"]:
        return _json_response({"error": "摘要不能为空"}, 400)
    group_id = unit["group_id"]
    _, units = _load_memory_units_file(_memory_unit_file(data_dir, group_id))
    if any(item.get("unit_id") == unit["unit_id"] for item in units):
        return _json_response({"error": "记忆单元已存在"}, 409)
    if any(
        normalize_summary(str(item.get("summary") or "")) == normalize_summary(unit["summary"])
        for item in units
    ):
        return _json_response(
            {
                "success": True,
                "unit": next(
                    item
                    for item in units
                    if normalize_summary(str(item.get("summary") or ""))
                    == normalize_summary(unit["summary"])
                ),
            },
            200,
        )
    units.append(unit)
    _save_memory_units_file(data_dir, group_id, units)
    _queue_memory_reconcile(data_dir, group_ids=[group_id], unit_ids=[unit["unit_id"]])
    return _json_response({"success": True, "unit": unit}, 201)


@handle_api_errors
async def api_persona_memory_unit_put(request: web.Request, data_dir: Path) -> web.Response:
    """Update a MemoryUnit record."""
    from urllib.parse import unquote

    if _memory_units_applying(data_dir):
        return _json_response({"error": "记忆清理正在应用"}, 409)

    unit_id = unquote(str(request.match_info.get("unit_id", ""))).strip()
    if not unit_id:
        return _json_response({"error": "缺少记忆单元 ID"}, 400)
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "Invalid JSON"}, 400)
    if not isinstance(body, dict):
        return _json_response({"error": "Invalid JSON"}, 400)

    base_dir = _memory_units_dir(data_dir)
    old_group = ""
    old_units: list[dict[str, Any]] = []
    old_index = -1
    found = False
    if base_dir.exists():
        for path in base_dir.glob("*.json"):
            group_id, units = _load_memory_units_file(path)
            for index, unit in enumerate(units):
                if str(unit.get("unit_id")) == unit_id:
                    old_group, old_units, old_index, found = group_id, units, index, True
                    break
            if found:
                break
    if not found:
        return _json_response({"error": "记忆单元不存在"}, 404)

    updated = _normalize_memory_unit(
        {**old_units[old_index], **body, "unit_id": unit_id}, old_group
    )
    if not updated["summary"]:
        return _json_response({"error": "摘要不能为空"}, 400)

    old_units.pop(old_index)
    _save_memory_units_file(data_dir, old_group, old_units)
    new_group = updated["group_id"]
    _, new_units = _load_memory_units_file(_memory_unit_file(data_dir, new_group))
    new_units = [unit for unit in new_units if str(unit.get("unit_id")) != unit_id]
    new_units.append(updated)
    _save_memory_units_file(data_dir, new_group, new_units)
    _queue_memory_reconcile(data_dir, group_ids=[old_group, new_group], unit_ids=[unit_id])
    return _json_response({"success": True, "unit": updated})


@handle_api_errors
async def api_persona_memory_unit_delete(request: web.Request, data_dir: Path) -> web.Response:
    """Delete a MemoryUnit record."""
    from urllib.parse import unquote

    if _memory_units_applying(data_dir):
        return _json_response({"error": "记忆清理正在应用"}, 409)

    unit_id = unquote(str(request.match_info.get("unit_id", ""))).strip()
    if not unit_id:
        return _json_response({"error": "缺少记忆单元 ID"}, 400)

    base_dir = _memory_units_dir(data_dir)
    if not base_dir.exists():
        return _json_response({"error": "记忆单元不存在"}, 404)
    for path in base_dir.glob("*.json"):
        group_id, units = _load_memory_units_file(path)
        kept = [unit for unit in units if str(unit.get("unit_id")) != unit_id]
        if len(kept) != len(units):
            _save_memory_units_file(data_dir, group_id, kept)
            _queue_memory_reconcile(data_dir, group_ids=[group_id], unit_ids=[unit_id])
            return _json_response({"success": True})
    return _json_response({"error": "记忆单元不存在"}, 404)


@handle_api_errors
async def api_persona_memory_dedupe_scan(request: web.Request, data_dir: Path) -> web.Response:
    if not _is_persona_running(data_dir):
        return _json_response({"error": "请先启动当前人格"}, 409)
    status = _dedupe_status(data_dir)
    if status.get("status") in _ACTIVE_DEDUPE_STATES:
        return _json_response({"error": "已有记忆清理任务", "status": status}, 409)
    job_id = f"dedupe_{uuid4().hex}"
    job_dir = _memory_dedupe_dir(data_dir)
    _atomic_write_json(job_dir / "request.json", {"action": "scan", "job_id": job_id})
    _atomic_write_json(
        job_dir / "status.json", {"job_id": job_id, "status": "queued", "progress": 0}
    )
    return _json_response({"job_id": job_id, "status": "queued"}, 202)


@handle_api_errors
async def api_persona_memory_dedupe_status(request: web.Request, data_dir: Path) -> web.Response:
    status = _dedupe_status(data_dir) or {"status": "idle"}
    return _json_response({**status, "worker_running": _is_persona_running(data_dir)})


@handle_api_errors
async def api_persona_memory_dedupe_apply(request: web.Request, data_dir: Path) -> web.Response:
    if not _is_persona_running(data_dir):
        return _json_response({"error": "请先启动当前人格"}, 409)
    body = await request.json()
    job_id = str(body.get("job_id") or "") if isinstance(body, dict) else ""
    status = _dedupe_status(data_dir)
    if status.get("status") != "ready" or status.get("job_id") != job_id:
        return _json_response({"error": "扫描报告不可应用"}, 409)
    _atomic_write_json(
        _memory_dedupe_dir(data_dir) / "request.json", {"action": "apply", "job_id": job_id}
    )
    _atomic_write_json(
        _memory_dedupe_dir(data_dir) / "status.json",
        {"job_id": job_id, "status": "queued", "phase": "apply"},
    )
    return _json_response({"job_id": job_id, "status": "queued"}, 202)


@handle_api_errors
async def api_persona_memory_dedupe_report(request: web.Request, data_dir: Path) -> web.Response:
    job_id = str(_dedupe_status(data_dir).get("job_id") or "")
    if not job_id:
        return _json_response({"error": "暂无扫描报告"}, 404)
    report = _read_json_dict(data_dir / "logs" / "memory-dedupe" / f"{job_id}.json")
    if not report:
        return _json_response({"error": "扫描报告不存在"}, 404)
    return _json_response(report)


@handle_api_errors
async def api_persona_memory_viz(request: web.Request, data_dir: Path) -> web.Response:
    """GET /api/persona/memory-viz — 记忆可视化数据聚合接口。

    Query params:
        group_id     : 按群过滤（为空则全部）
        basic_limit  : 基础记忆条数上限（默认 500，最大 2000）
    """
    group_filter = request.query.get("group_id", "").strip()
    limit_basic = min(int(request.query.get("basic_limit", "500")), 2000)

    paths = PersonaConfigPaths(data_dir)

    # ── 1. 基础记忆：按群+天聚合为柱状图数据 ──
    archive_dir = paths.dir / "archive"
    all_groups: set[str] = set()
    # day_bucket[date][group_id] = {human: N, assistant: N, system: N}
    day_bucket: dict[str, dict[str, dict[str, int]]] = {}
    # 最近 N 条明细（仅用于 tooltip 展示）
    recent_entries: list[dict[str, Any]] = []

    if archive_dir.exists():
        for path in archive_dir.glob("*.jsonl"):
            gid = path.stem
            all_groups.add(gid)
            if group_filter and gid != group_filter:
                continue
            try:
                with path.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            ts = data.get("timestamp", "")
                            role = data.get("role", "human")
                            day = ts[:10] if ts else "unknown"
                            if day not in day_bucket:
                                day_bucket[day] = {}
                            if gid not in day_bucket[day]:
                                day_bucket[day][gid] = {"human": 0, "assistant": 0, "system": 0}
                            day_bucket[day][gid][role] = day_bucket[day][gid].get(role, 0) + 1

                            recent_entries.append(
                                {
                                    "group_id": gid,
                                    "speaker_name": data.get("speaker_name", ""),
                                    "role": role,
                                    "content": data.get("content", "")[:120],
                                    "timestamp": ts,
                                }
                            )
                        except (json.JSONDecodeError, TypeError):
                            continue
            except OSError:
                continue

    recent_entries.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
    recent_entries = recent_entries[:limit_basic]

    days_sorted = sorted(day_bucket.keys())
    groups_in_data = sorted({g for bucket in day_bucket.values() for g in bucket})

    return _json_response(
        {
            "groups": sorted(all_groups),
            "basic_timeline": {
                "days": days_sorted,
                "groups": groups_in_data,
                "buckets": day_bucket,
                "recent": recent_entries,
            },
        }
    )


@handle_api_errors
async def api_persona_conversation_history_get(
    request: web.Request, data_dir: Path
) -> web.Response:
    """GET /api/persona/conversations — 返回对话历史（分页，支持搜索筛选）。"""
    group_id = request.query.get("group_id", "").strip()
    limit = min(int(request.query.get("limit", "50")), 200)
    offset = max(int(request.query.get("offset", "0")), 0)
    search = request.query.get("search", "").strip().lower()
    speaker = request.query.get("speaker", "").strip().lower()
    start_time = request.query.get("start", "").strip()
    end_time = request.query.get("end", "").strip()

    paths = PersonaConfigPaths(data_dir)

    archive_dir = paths.dir / "archive"
    compressed_source_index = _load_compressed_memory_source_index(paths, group_id)

    # 获取所有群组
    groups = []
    if archive_dir.exists():
        for f in archive_dir.glob("*.jsonl"):
            groups.append(f.stem)

    runtime_messages = _load_runtime_basic_memory_messages(paths, group_id)
    runtime_groups = {
        str(message.get("group_id") or "")
        for message in runtime_messages
        if str(message.get("group_id") or "").strip()
    }
    groups = sorted(set(groups) | runtime_groups)

    target_files = []
    if archive_dir.exists():
        if group_id:
            target_file = archive_dir / f"{group_id}.jsonl"
            if target_file.exists():
                target_files.append(target_file)
        else:
            target_files = sorted(archive_dir.glob("*.jsonl"))

    if not target_files and not runtime_messages:
        return _json_response({"messages": [], "groups": groups, "total": 0})

    has_filters = bool(search or speaker or start_time or end_time)
    max_offset = 1_000
    if offset > max_offset:
        raise web.HTTPBadRequest(text=f"offset must not exceed {max_offset}")

    if has_filters:
        # Stream the complete archive and retain only the requested page. A large
        # archive must never be materialized as one in-memory list for a search.
        max_offset = 1_000
        if offset > max_offset:
            raise web.HTTPBadRequest(text=f"offset must not exceed {max_offset}")
        need = offset + limit
        newest: list[tuple[str, int, dict[str, Any]]] = []
        sequence = 0
        total = 0
        runtime_by_key = {
            _conversation_entry_key(message): _compact_history_entry(message)
            for message in runtime_messages
        }

        def keep_matching(entry: dict[str, Any]) -> None:
            nonlocal sequence, total
            if not _conversation_message_matches_filters(
                entry,
                search=search,
                speaker=speaker,
                start_time=start_time,
                end_time=end_time,
            ):
                return
            total += 1
            sequence += 1
            item = (str(entry.get("timestamp") or ""), sequence, _compact_history_entry(entry))
            if len(newest) < need:
                heappush(newest, item)
            elif item[:2] > newest[0][:2]:
                heapreplace(newest, item)

        for fpath in target_files:
            g_id = fpath.stem
            try:
                with fpath.open("r", encoding="utf-8") as f:
                    for line in f:
                        if not line.strip():
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(entry, dict):
                            continue
                        entry["group_id"] = g_id
                        if not entry.get("tags"):
                            entry["tags"] = []
                        key = _conversation_entry_key(entry)
                        runtime = runtime_by_key.pop(key, None)
                        keep_matching({**entry, **runtime} if runtime else entry)
            except OSError:
                continue

        for entry in runtime_by_key.values():
            keep_matching(entry)

        newest.sort(key=lambda item: item[:2], reverse=True)
        messages = [item[2] for item in newest[offset : offset + limit]]
        _annotate_memory_compression(messages, compressed_source_index)
    else:
        # Count records without deserializing them, then retain only the requested
        # page while scanning each file backwards.
        total = 0
        for fpath in target_files:
            try:
                with fpath.open("rb") as f:
                    total += sum(1 for _ in f)
            except OSError:
                continue

        need = offset + limit
        newest: list[tuple[str, int, dict[str, Any]]] = []
        sequence = 0
        runtime_by_key = {
            _conversation_entry_key(message): _compact_history_entry(message)
            for message in runtime_messages
        }

        def keep_recent(entry: dict[str, Any]) -> None:
            nonlocal sequence
            sequence += 1
            item = (
                str(entry.get("timestamp") or ""),
                sequence,
                _compact_history_entry(entry),
            )
            if len(newest) < need:
                heappush(newest, item)
            elif item[:2] > newest[0][:2]:
                heapreplace(newest, item)

        for fpath in target_files:
            g_id = fpath.stem
            try:
                for line in _iter_tail_lines(fpath, need):
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(entry, dict):
                        continue
                    entry["group_id"] = g_id
                    if not entry.get("tags"):
                        entry["tags"] = []
                    key = _conversation_entry_key(entry)
                    runtime = runtime_by_key.pop(key, None)
                    keep_recent({**entry, **runtime} if runtime else entry)
            except OSError:
                continue

        for entry in runtime_by_key.values():
            keep_recent(entry)
        total += len(runtime_by_key)

        newest.sort(key=lambda item: item[:2], reverse=True)
        messages = [item[2] for item in newest[offset : offset + limit]]
        _annotate_memory_compression(messages, compressed_source_index)

    return _json_response(
        {
            "messages": messages,
            "groups": sorted(groups),
            "total": total,
            "offset": offset,
            "limit": limit,
        }
    )


@handle_api_errors
async def api_persona_conversation_history_delete(
    request: web.Request, data_dir: Path
) -> web.Response:
    """DELETE /api/persona/conversations — delete one archived/runtime message."""
    group_id = request.query.get("group_id", "").strip()
    key = _conversation_key_from_query(request)
    if not key:
        raise web.HTTPBadRequest(text="missing conversation message identifier")

    paths = PersonaConfigPaths(data_dir)
    if _is_persona_running(data_dir):
        raise web.HTTPConflict(text="stop the persona before deleting archived history")
    archive_dir = paths.dir / "archive"

    deleted_archive = 0
    if archive_dir.exists():
        target_files: list[Path]
        if group_id:
            target_files = [archive_dir / f"{_safe_memory_name(group_id)}.jsonl"]
        else:
            target_files = sorted(archive_dir.glob("*.jsonl"))
        for path in target_files:
            deleted_archive += _rewrite_jsonl_without_conversation_key(path, path.stem, key)

    deleted_runtime = _delete_runtime_basic_memory_message(paths, key, group_id)
    deleted = deleted_archive + deleted_runtime
    return _json_response(
        {
            "success": True,
            "deleted": deleted,
            "deleted_archive": deleted_archive,
            "deleted_runtime": deleted_runtime,
        }
    )
